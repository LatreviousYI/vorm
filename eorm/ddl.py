from __future__ import annotations

import typing
from dataclasses import dataclass
from typing import Any


@dataclass
class IntrospectedColumn:
    """Mirrors a single column row from INFORMATION_SCHEMA.COLUMNS."""

    column_name: str
    data_type: str
    is_nullable: bool
    column_default: str | None = None


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
