from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar, cast

from pydantic import BaseModel, ConfigDict

from eorm.ddl import Index
from eorm.exceptions import ModelDefinitionError
from eorm.expression import Column
from eorm.fields import ColumnInfo, get_column_info

if TYPE_CHECKING:
    from eorm.dialects.base import AbstractDialect

logger = logging.getLogger("eorm")

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

        # -- 索引 -------------------------------------------------------
        indexes: list[Index] = []

        # Meta.indexes 声明的联合索引
        meta_indexes = getattr(meta, "indexes", None) or []
        for idx in meta_indexes:
            if isinstance(idx, Index):
                indexes.append(idx)

        # Field(index=True) 单列索引 → 自动转为 Index
        for field_name, info in column_info.items():
            if info.index and not info.unique:  # unique 已生成 UNIQUE 约束，跳过
                indexes.append(Index(fields=(field_name,), unique=False))

        cls.__indexes__ = indexes

        return cls

    def __getattribute__(cls, name: str) -> Any:
        """将 ORM 字段名拦截为 Column 对象，避免 setattr 覆盖原始类型标注。

        只拦截类级别访问（如 ``User.name``），
        不影响实例访问（``user.name``）。
        为什么要绕一圈走 __dict__？ 避免无限递归。
        如果在 __getattribute__ 里写 cls.__columns__，这个 .
        又会触发 __getattribute__ 自身，形成死循环。
        所以先用 super() 跳到 type.__getattribute__ 拿到原始 __dict__，
        再从里面取 "__columns__"，安全绕过拦截。
        简单说：__new__ 负责"写入"，__getattribute__ 负责"读取"。
        写入在前（类定义时），读取在后（访问类属性时），
        所以 __dict__ 里一定有这个 key。
        """
        # 绕开自身，直接从 type 拿原始 __dict__
        cls_dict = super().__getattribute__("__dict__")
        # 从原始字典里取出 Column 映射
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
    __indexes__: ClassVar[list[Index]]

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
        表存在时为新增字段和修改列合并为一条 ``ALTER TABLE``；
        随后同步缺失的索引（``CREATE INDEX``，只增不删）。
        永不删除列、表或索引。
        """
        existing = await dialect.introspect_columns(cls.__table__)

        if not existing:
            sql = dialect.build_create_table(cls)
            logger.info("同步表 %s：创建表", cls.__table__)
            await dialect.execute(sql, [])
        else:
            # 收集新增列和待修改列
            missing: list[str] = []
            modified: list[tuple[str, Any]] = []  # (field_name, IntrospectedColumn)

            for field_name, info in cls.__column_info__.items():
                expected_name = (info.column_name or field_name).lower()
                matched = existing.get(expected_name)
                if matched is None:
                    missing.append(field_name)
                else:
                    modified.append((field_name, matched))

            if missing or modified:
                logger.info(
                    "同步表 %s：新增 %d 列，修改 %d 列",
                    cls.__table__,
                    len(missing),
                    len(modified),
                )
                sql = dialect.build_sync_alter(cls, missing, modified)
                if sql:
                    await dialect.execute(sql, [])
                # 额外 DDL（如 PG 创建序列）
                for extra_sql in dialect.build_post_alter(cls, modified):
                    logger.info("同步表 %s：执行额外 DDL", cls.__table__)
                    await dialect.execute(extra_sql, [])

        # -- 索引同步（只增不删） --
        existing_indexes = await dialect.introspect_indexes(cls.__table__)
        for sql in dialect.build_sync_indexes(cls, existing_indexes):
            logger.info("同步表 %s：创建索引", cls.__table__)
            await dialect.execute(sql, [])
