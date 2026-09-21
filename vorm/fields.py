from __future__ import annotations

from typing import Any, cast

from pydantic import fields as _pydantic_fields
from pydantic_core import PydanticUndefined


class ColumnInfo:
    """Extra ORM metadata attached to a model field."""

    __slots__ = (
        "primary_key",
        "auto_increment",
        "column_name",
        "nullable",
        "unique",
        "index",
        "max_length",
        "db_type",
        "timestamp_behavior",
        "comment",
    )

    def __init__(
        self,
        *,
        primary_key: bool = False,
        auto_increment: bool = False,
        column_name: str | None = None,
        nullable: bool = True,
        unique: bool = False,
        index: bool = False,
        max_length: int | None = None,
        db_type: str | None = None,
        timestamp_behavior: str | None = None,
        comment: str | None = None,
    ) -> None:
        self.primary_key = primary_key
        self.auto_increment = auto_increment
        self.column_name = column_name
        self.nullable = nullable
        self.unique = unique
        self.index = index
        self.max_length = max_length
        self.db_type = db_type
        self.timestamp_behavior = timestamp_behavior
        self.comment = comment


def Field(
    default: Any = PydanticUndefined,
    *,
    primary_key: bool = False,
    auto_increment: bool = False,
    column_name: str | None = None,
    nullable: bool = True,
    unique: bool = False,
    index: bool = False,
    max_length: int | None = None,
    db_type: str | None = None,
    timestamp_behavior: str | None = None,
    comment: str | None = None,
    **pydantic_kwargs: Any,
) -> Any:
    """Drop-in replacement for pydantic.Field that carries ORM column metadata."""
    if default is PydanticUndefined and primary_key and auto_increment:
        default = None

    if timestamp_behavior not in (None, "create", "update", "both"):
        raise ValueError("timestamp_behavior must be one of None, 'create', 'update', or 'both'")

    column_info = ColumnInfo(
        primary_key=primary_key,
        auto_increment=auto_increment,
        column_name=column_name,
        nullable=False if primary_key else nullable,
        unique=unique,
        index=index,
        max_length=max_length,
        db_type=db_type,
        timestamp_behavior=timestamp_behavior,
        comment=comment,
    )
    # max_length 同时是 ORM 参数（VARCHAR(N)）和 Pydantic 参数（字符串校验），
    # 需要传给两边，否则 Pydantic 收不到长度校验
    if max_length is not None:
        pydantic_kwargs.setdefault("max_length", max_length)

    json_schema_extra = pydantic_kwargs.pop("json_schema_extra", None)
    if json_schema_extra is None:
        json_schema_extra = {}
    if not isinstance(json_schema_extra, dict):
        raise TypeError("vorm.Field only supports dict json_schema_extra")
    json_schema_extra = {**json_schema_extra, "vorm_column": column_info}
    if default is PydanticUndefined:
        return cast(
            Any,
            _pydantic_fields.Field(
                json_schema_extra=cast(Any, json_schema_extra),
                **pydantic_kwargs,
            ),
        )
    return cast(
        Any,
        _pydantic_fields.Field(
            default,
            json_schema_extra=cast(Any, json_schema_extra),
            **pydantic_kwargs,
        ),
    )


def get_column_info(field_info: _pydantic_fields.FieldInfo) -> ColumnInfo | None:
    """Extract ColumnInfo from a Pydantic FieldInfo, if present."""
    for item in field_info.metadata:
        if isinstance(item, ColumnInfo):
            return item
    if isinstance(field_info.json_schema_extra, dict):
        item = field_info.json_schema_extra.get("vorm_column")
        if isinstance(item, ColumnInfo):
            return item
    return None
