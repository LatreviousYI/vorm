from __future__ import annotations

import json as _json_mod
import typing as _typing
from dataclasses import dataclass
from typing import Any, Generic, TypeVar, cast

from eorm.exceptions import DoesNotExist, MultipleObjectsReturned
from eorm.expression import Column, Expression, OrderExpression
from eorm.model import Model
from eorm.row import Row

ModelT = TypeVar("ModelT", bound=Model)


def _resolve_annotation_base(annotation: Any) -> type:
    """Unwrap Optional[X] and parameterised generics to return the base type."""
    args = _typing.get_args(annotation)
    if args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    origin = _typing.get_origin(annotation)
    if origin is not None:
        return origin
    return annotation


@dataclass
class JoinInfo:
    """一条 JOIN 的元信息。"""

    model: type[Model]
    on: Expression
    join_type: str = "INNER"  # INNER | LEFT


class QuerySet(Generic[ModelT]):
    def __init__(self, session: Any, model: type[ModelT]) -> None:
        self.session = session
        self.model = model
        self.filters: list[Expression] = []
        self.orderings: list[OrderExpression] = []
        self.limit_count: int | None = None
        self.offset_count: int | None = None
        self.joins: list[JoinInfo] = []

    def _clone(self) -> QuerySet[ModelT]:
        clone = type(self)(self.session, self.model)
        clone.filters = [*self.filters]
        clone.orderings = [*self.orderings]
        clone.limit_count = self.limit_count
        clone.offset_count = self.offset_count
        clone.joins = [*self.joins]
        return clone

    def filter(self, *exprs: Any) -> QuerySet[ModelT]:
        clone = self._clone()
        clone.filters.extend(exprs)
        return clone

    def order_by(self, *orderings: Any) -> QuerySet[ModelT]:
        clone = self._clone()
        for ordering in orderings:
            clone.orderings.append(ordering.asc() if isinstance(ordering, Column) else ordering)
        return clone

    def limit(self, count: int) -> QuerySet[ModelT]:
        clone = self._clone()
        clone.limit_count = count
        return clone

    def offset(self, count: int) -> QuerySet[ModelT]:
        clone = self._clone()
        clone.offset_count = count
        return clone

    def join(self, model: type[Model], *, on: Expression, type: str = "INNER") -> QuerySet[ModelT]:
        """关联另一张表。

        :param model: 要 JOIN 的模型类
        :param on: JOIN 条件表达式（如 ``User.id == Post.user_id``）
        :param type: JOIN 类型，``"INNER"``（默认）或 ``"LEFT"``
        """
        clone = self._clone()
        clone.joins.append(JoinInfo(model, on, type.upper()))
        return clone

    def build_sql(self) -> tuple[str, list[Any]]:
        return cast(tuple[str, list[Any]], self.session.dialect.build_select(self))

    # -- 执行 ---------------------------------------------------------------

    def _table_names(self) -> list[str]:
        return [self.model.__table__] + [j.model.__table__ for j in self.joins]

    async def all(self) -> list[Any]:
        sql, params = self.build_sql()
        rows = await self.session.dialect.fetch(sql, params)
        if self.joins:
            table_names = self._table_names()
            return [Row(row, table_names) for row in rows]

        # 将 JSON 字符串反序列化为 Python 对象再交给 Pydantic 校验
        for row in rows:
            for field_name, field_info in self.model.model_fields.items():
                annotation = field_info.annotation
                base = _resolve_annotation_base(annotation)
                if base in (dict, list) and field_name in row:
                    val = row[field_name]
                    if isinstance(val, str):
                        try:
                            row[field_name] = _json_mod.loads(val)
                        except (_json_mod.JSONDecodeError, TypeError, ValueError):
                            pass
        return [self.model.model_validate(row) for row in rows]

    async def first(self) -> Any:
        rows = await self.limit(1).all()
        return rows[0] if rows else None

    async def one(self) -> Any:
        rows = await self.limit(2).all()
        if not rows:
            raise DoesNotExist(f"No {self.model.__name__} row matched the query")
        if len(rows) > 1:
            raise MultipleObjectsReturned(f"Multiple {self.model.__name__} rows matched the query")
        return rows[0]

    async def count(self) -> int:
        sql, params = self.session.dialect.build_select(self, count=True)
        row = await self.session.dialect.fetchrow(sql, params)
        return int(row["count"]) if row is not None else 0

    async def exists(self) -> bool:
        return await self.count() > 0

    async def update(self, **values: Any) -> int:
        """按条件批量更新，无需先查询。返回受影响行数。

        用法::

            row_count = await session.query(User).filter(User.age.is_null()).update(age=0)
        """
        if not values:
            return 0
        sql, params = self.session.dialect.build_update_by_query(self, values)
        result = await self.session.dialect.execute(sql, params)
        return int(result) if result is not None else 0

    async def delete(self) -> int:
        """按条件批量删除，无需先查询。返回受影响行数。

        用法::

            row_count = await session.query(User).filter(User.age < 18).delete()
        """
        sql, params = self.session.dialect.build_delete_by_query(self)
        result = await self.session.dialect.execute(sql, params)
        return int(result) if result is not None else 0
