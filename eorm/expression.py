from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from eorm.dialects.base import AbstractDialect


@dataclass(frozen=True)
class OrderExpression:
    column: Column
    direction: str = "ASC"

    def render(self, dialect: AbstractDialect) -> str:
        return f"{self.column.render(dialect)} {self.direction}"


class Expression:
    def render(self, dialect: AbstractDialect, params: list[Any]) -> str:
        raise NotImplementedError

    def __and__(self, other: Expression) -> CombinedExpression:
        return CombinedExpression("AND", self, other)

    def __or__(self, other: Expression) -> CombinedExpression:
        return CombinedExpression("OR", self, other)


@dataclass(frozen=True)
class BinaryExpression(Expression):
    column: Any  # Column or Expression (e.g. JSONPathExpression)
    operator: str
    value: Any

    def _render_side(self, side: Any, dialect: AbstractDialect, params: list[Any]) -> str:
        """Render a left or right side of the expression.

        Column.render takes only ``dialect``; Expression.render takes ``(dialect, params)``.
        """
        if isinstance(side, Column):
            return side.render(dialect)
        if isinstance(side, Expression):
            return side.render(dialect, params)
        return str(side)

    def render(self, dialect: AbstractDialect, params: list[Any]) -> str:
        if isinstance(self.value, Column):
            # 列对列比较（JOIN ON 条件等），不需要参数绑定
            return (
                f"{self._render_side(self.column, dialect, params)} "
                f"{self.operator} {self.value.render(dialect)}"
            )
        if isinstance(self.value, Expression):
            # 表达式对值比较（如 JSONPathExpression == "value"）
            params.append(self.value)
            placeholder = dialect.render_placeholder(len(params))
            return (
                f"{self._render_side(self.column, dialect, params)} {self.operator} {placeholder}"
            )
        params.append(self.value)
        placeholder = dialect.render_placeholder(len(params))
        return f"{self._render_side(self.column, dialect, params)} {self.operator} {placeholder}"


@dataclass(frozen=True)
class InExpression(Expression):
    column: Column
    values: tuple[Any, ...]

    def render(self, dialect: AbstractDialect, params: list[Any]) -> str:
        if not self.values:
            return "1 = 0"
        placeholders: list[str] = []
        for value in self.values:
            params.append(value)
            placeholders.append(dialect.render_placeholder(len(params)))
        return f"{self.column.render(dialect)} IN ({', '.join(placeholders)})"


@dataclass(frozen=True)
class NullExpression(Expression):
    column: Column
    is_null: bool = True

    def render(self, dialect: AbstractDialect, params: list[Any]) -> str:
        operator = "IS NULL" if self.is_null else "IS NOT NULL"
        return f"{self.column.render(dialect)} {operator}"


@dataclass(frozen=True)
class LikeExpression(Expression):
    column: Column
    pattern: str

    def render(self, dialect: AbstractDialect, params: list[Any]) -> str:
        params.append(self.pattern)
        placeholder = dialect.render_placeholder(len(params))
        return f"{self.column.render(dialect)} LIKE {placeholder}"


@dataclass(frozen=True)
class BetweenExpression(Expression):
    column: Column
    low: Any
    high: Any

    def render(self, dialect: AbstractDialect, params: list[Any]) -> str:
        params.append(self.low)
        low_ph = dialect.render_placeholder(len(params))
        params.append(self.high)
        high_ph = dialect.render_placeholder(len(params))
        return f"{self.column.render(dialect)} BETWEEN {low_ph} AND {high_ph}"


@dataclass(frozen=True)
class JSONPathExpression(Expression):
    """A JSON path extraction like ``column->>'$.key'`` that can be compared."""

    column: Column
    path: str
    as_text: bool = True  # True → ->>, False → ->

    def render(self, dialect: AbstractDialect, params: list[Any]) -> str:
        col_sql = self.column.render(dialect)
        return dialect.render_json_extract(col_sql, self.path, self.as_text)

    def _binary(self, operator: str, value: Any) -> BinaryExpression:
        return BinaryExpression(self, operator, value)

    def __eq__(self, other: Any) -> BinaryExpression:  # type: ignore[override]
        return self._binary("=", other)

    def __ne__(self, other: Any) -> BinaryExpression:  # type: ignore[override]
        return self._binary("!=", other)

    def __gt__(self, other: Any) -> BinaryExpression:
        return self._binary(">", other)

    def __ge__(self, other: Any) -> BinaryExpression:
        return self._binary(">=", other)

    def __lt__(self, other: Any) -> BinaryExpression:
        return self._binary("<", other)

    def __le__(self, other: Any) -> BinaryExpression:
        return self._binary("<=", other)


@dataclass(frozen=True)
class CombinedExpression(Expression):
    operator: str
    left: Expression
    right: Expression

    def render(self, dialect: AbstractDialect, params: list[Any]) -> str:
        left = self.left.render(dialect, params)
        right = self.right.render(dialect, params)
        return f"({left} {self.operator} {right})"


@dataclass(frozen=True)
class Column:
    model: type
    field_name: str
    column_name: str

    def render(self, dialect: AbstractDialect) -> str:
        table = dialect.quote_identifier(self.model.__table__)
        col = dialect.quote_identifier(self.column_name)
        return f"{table}.{col}"

    def asc(self) -> OrderExpression:
        return OrderExpression(self, "ASC")

    def desc(self) -> OrderExpression:
        return OrderExpression(self, "DESC")

    def json_path(self, path: str, *, as_text: bool = True) -> JSONPathExpression:
        """Create a JSON path extraction expression.

        ``as_text=True`` (default) uses ``->>``, returning text.
        ``as_text=False`` uses ``->``, returning JSON/JSONB.
        """
        return JSONPathExpression(self, path, as_text)

    def in_(self, values: Iterable[Any]) -> InExpression:
        return InExpression(self, tuple(values))

    def is_null(self) -> NullExpression:
        return NullExpression(self, True)

    def is_not_null(self) -> NullExpression:
        return NullExpression(self, False)

    def _binary(self, operator: str, value: Any) -> BinaryExpression:
        return BinaryExpression(self, operator, value)

    def __eq__(self, other: Any) -> BinaryExpression:  # type: ignore[override]
        return self._binary("=", other)

    def __ne__(self, other: Any) -> BinaryExpression:  # type: ignore[override]
        return self._binary("!=", other)

    def __gt__(self, other: Any) -> BinaryExpression:
        return self._binary(">", other)

    def __ge__(self, other: Any) -> BinaryExpression:
        return self._binary(">=", other)

    def __lt__(self, other: Any) -> BinaryExpression:
        return self._binary("<", other)

    def __le__(self, other: Any) -> BinaryExpression:
        return self._binary("<=", other)

    def like(self, pattern: str) -> LikeExpression:
        return LikeExpression(self, pattern)

    def between(self, low: Any, high: Any) -> BetweenExpression:
        return BetweenExpression(self, low, high)
