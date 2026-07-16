from __future__ import annotations

import enum
import typing
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Index:
    """索引定义，在 ``Meta.indexes`` 中声明。"""

    fields: tuple[str, ...]  # Python 属性名
    unique: bool = False
    name: str | None = None  # None → 自动生成

    def index_name(self, table: str) -> str:
        """自动生成或返回显式索引名。"""
        if self.name is not None:
            return self.name
        prefix = "unq" if self.unique else "ix"
        fields_part = "_".join(self.fields)
        return f"{prefix}_{table}_{fields_part}"


@dataclass
class IntrospectedColumn:
    """Mirrors a single column row from INFORMATION_SCHEMA.COLUMNS."""

    column_name: str
    data_type: str
    is_nullable: bool
    column_default: str | None = None
    column_comment: str | None = None
    is_auto_increment: bool = False


def resolve_base_type(annotation: Any) -> type:
    """Unwrap ``Optional[X]`` / ``X | None`` and parameterised generics.

    ``Optional[str]`` → ``str``; ``dict[str, Any]`` → ``dict``.
    """
    args = typing.get_args(annotation)
    if args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return non_none[0]
    origin = typing.get_origin(annotation)
    if origin is not None:
        return origin
    return annotation


def is_optional(annotation: Any) -> bool:
    """Return ``True`` if the annotation accepts ``None``."""
    args = typing.get_args(annotation)
    if args:
        return type(None) in args
    return False


def is_enum_type(annotation: Any) -> bool:
    """Return ``True`` if the annotation resolves to an ``enum.Enum`` subclass."""
    base = resolve_base_type(annotation)
    return isinstance(base, type) and issubclass(base, enum.Enum)


def get_enum_values(annotation: Any) -> list[Any]:
    """Return the list of member values from an ``enum.Enum`` subclass."""
    base = resolve_base_type(annotation)
    return [member.value for member in base.__members__.values()]
