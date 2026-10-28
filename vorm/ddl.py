from __future__ import annotations

import enum
import hashlib
import typing
from dataclasses import dataclass
from types import UnionType
from typing import Any, cast


@dataclass(frozen=True)
class Index:
    """索引定义，在 ``Meta.indexes`` 中声明。"""

    fields: tuple[str, ...]  # Python 属性名
    unique: bool = False
    name: str | None = None  # None → 自动生成

    def index_name(self, table: str) -> str:
        """自动生成或返回显式索引名。

        超过 63 字节时确定性截断：保留前缀 + 原名 md5 后 8 位十六进制，
        如 ``ix_long_table_long_field_a1b2c3d4``。同一输入永远得到同一结果，
        ``sync_table()`` 的已有索引比对因此不受影响；共享前缀的不同长名
        后缀不同，不会互相冲突。显式 ``name`` 同样适用。
        """
        if self.name is not None:
            return _fit_identifier(self.name)
        prefix = "unq" if self.unique else "ix"
        fields_part = "_".join(self.fields)
        return _fit_identifier(f"{prefix}_{table}_{fields_part}")


# MySQL 标识符上限 64 字符（超长直接报 1059），PostgreSQL 为 63 字节
# （NAMEDATALEN-1，超长被静默截断）。取两者交集 63 字节。
MAX_IDENTIFIER_BYTES = 63

# 后缀 = 1 个下划线 + 8 位十六进制（md5 前 8 位，32 bit 摘要空间）
_HASH_SUFFIX_LEN = 9


def _fit_identifier(name: str, max_bytes: int = MAX_IDENTIFIER_BYTES) -> str:
    """将标识符压缩到 ``max_bytes`` 以内，超长时追加确定性哈希后缀。

    按字节（UTF-8）而非字符计数，与 PostgreSQL 的 NAMEDATALEN 语义一致；
    截断发生在字节边界，残缺的多字节字符会被丢弃。
    """
    raw = name.encode("utf-8")
    if len(raw) <= max_bytes:
        return name
    digest = hashlib.md5(raw).hexdigest()[:_HASH_SUFFIX_LEN - 1]
    prefix = raw[: max_bytes - _HASH_SUFFIX_LEN].decode("utf-8", errors="ignore")
    return f"{prefix}_{digest}"


@dataclass
class IntrospectedColumn:
    """Mirrors a single column row from INFORMATION_SCHEMA.COLUMNS."""

    column_name: str
    data_type: str
    is_nullable: bool
    column_default: str | None = None
    column_comment: str | None = None
    is_auto_increment: bool = False
    extra: str | None = None

    @property
    def has_on_update_current_timestamp(self) -> bool:
        """Return whether introspection reported automatic timestamp updates."""
        return "on update current_timestamp" in (self.extra or "").lower()


def _postgresql_identifier(name: str) -> str:
    """Keep generated identifiers deterministic and within PG's 63-byte limit."""
    if len(name.encode()) <= 63:
        return name
    digest = hashlib.sha256(name.encode()).hexdigest()[:12]
    prefix = name.encode()[: 63 - len(digest) - 1].decode(errors="ignore")
    return f"{prefix}_{digest}"


def timestamp_trigger_name(table_name: str, column_name: str) -> str:
    """Return a deterministic name for a VORM-managed update trigger."""
    return _postgresql_identifier(f"vorm_ts_{table_name}_{column_name}_update")


def timestamp_function_name(table_name: str, column_name: str) -> str:
    """Return a deterministic name for a VORM-managed trigger function."""
    return _postgresql_identifier(f"vorm_ts_{table_name}_{column_name}_fn")


def resolve_base_type(annotation: Any) -> type[Any]:
    """Unwrap ``Optional[X]`` / ``X | None`` and parameterised generics.

    ``Optional[str]`` → ``str``; ``dict[str, Any]`` → ``dict``.
    """
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Union or isinstance(annotation, UnionType):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return resolve_base_type(non_none[0])
        return cast("type[Any]", annotation)
    if origin is not None:
        return cast("type[Any]", origin)
    return cast("type[Any]", annotation)


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
    if isinstance(base, type) and issubclass(base, enum.Enum):
        return [member.value for member in base.__members__.values()]
    return []
