from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from pydantic import BaseModel, ConfigDict

from eorm.exceptions import ModelDefinitionError
from eorm.expression import Column
from eorm.fields import ColumnInfo, get_column_info

if TYPE_CHECKING:
    from eorm.dialects.base import AbstractDialect

ModelMetaclass = type(BaseModel)


class ModelMeta(ModelMetaclass):
    def __new__(
        mcls,
        name: str,
        bases: tuple[type[Any], ...],
        namespace: dict[str, Any],
        **kwargs: Any,
    ) -> Any:
        cls = cast(Any, super().__new__(mcls, name, bases, namespace, **kwargs))
        if name == "Model" and cls.__module__ == __name__:
            return cls

        meta = getattr(cls, "Meta", None)
        table = getattr(meta, "table", None) or name.lower()

        columns: dict[str, Column] = {}
        column_info: dict[str, ColumnInfo] = {}
        pk_name: str | None = None

        for field_name, field_info in cls.model_fields.items():
            info = get_column_info(field_info) or ColumnInfo()
            column_name = info.column_name or field_name
            column = Column(cls, field_name, column_name)
            columns[field_name] = column
            column_info[field_name] = info
            if info.primary_key:
                if pk_name is not None:
                    raise ModelDefinitionError(f"{name} declares more than one primary key")
                pk_name = field_name

        cls.__table__ = table
        cls.__columns__ = columns
        cls.__column_info__ = column_info
        cls.__pk__ = pk_name

        return cls

    def __getattribute__(cls, name: str) -> Any:
        """将 ORM 字段名拦截为 Column 对象，避免 setattr 覆盖原始类型标注。

        只拦截类级别访问（如 ``User.name``），不影响实例访问（``user.name``）。
        """
        cls_dict = super().__getattribute__("__dict__")
        columns = cls_dict.get("__columns__")
        if columns is not None and name in columns:
            return columns[name]
        return super().__getattribute__(name)


class Model(BaseModel, metaclass=ModelMeta):
    model_config = ConfigDict(arbitrary_types_allowed=True, from_attributes=True)

    __table__: ClassVar[str]
    __columns__: ClassVar[dict[str, Column]]
    __column_info__: ClassVar[dict[str, ColumnInfo]]
    __pk__: ClassVar[str | None]

    @classmethod
    def table_name(cls) -> str:
        return cls.__table__

    @classmethod
    def primary_key_name(cls) -> str:
        if cls.__pk__ is None:
            raise ModelDefinitionError(f"{cls.__name__} does not declare a primary key")
        return cls.__pk__

    @classmethod
    def primary_key_column(cls) -> Column:
        return cls.__columns__[cls.primary_key_name()]

    def primary_key_value(self) -> Any:
        return getattr(self, self.primary_key_name())

    @classmethod
    async def sync_table(cls, dialect: AbstractDialect) -> None:
        """创建或同步数据库表（只增改不删）。

        表不存在时执行 ``CREATE TABLE IF NOT EXISTS``；
        表存在时为新增字段合并为一条 ``ALTER TABLE ADD COLUMN``；
        已有列若类型或 null 约束变化则生成对应的修改语句。
        永不删除列或表。
        """
        existing = await dialect.introspect_columns(cls.__table__)

        if not existing:
            sql = dialect.build_create_table(cls)
            await dialect.execute(sql, [])
            return

        # 收集新增列和待修改列
        missing: list[str] = []
        modified: list[tuple[str, object]] = []  # (field_name, IntrospectedColumn)
        pk_name = cls.__pk__

        for field_name, info in cls.__column_info__.items():
            expected_name = (info.column_name or field_name).lower()
            # 按字段名在 existing 中查找（大小写不敏感）
            matched = existing.get(expected_name)
            if matched is None:
                missing.append(field_name)
            elif field_name != pk_name:
                # PK 列属于结构性约束，不参与字段属性修改
                modified.append((field_name, matched))

        if missing:
            sql = dialect.build_add_columns(cls, missing)
            await dialect.execute(sql, [])

        if modified:
            sql = dialect.build_modify_columns(cls, modified)
            if sql:
                await dialect.execute(sql, [])
