from __future__ import annotations

import datetime as _dt
import decimal
import logging
from typing import Any, cast

import asyncmy

from eorm.ddl import IntrospectedColumn, resolve_base_type
from eorm.dialects.base import AbstractDialect
from eorm.fields import ColumnInfo

logger = logging.getLogger("eorm")


class MySQLDialect(AbstractDialect):
    quote_char = "`"

    def __init__(self, pool: asyncmy.Pool, *, _owns_pool: bool = True) -> None:
        self.pool = pool
        self._tx_conn: asyncmy.Connection | None = None
        self._owns_pool = _owns_pool

    def render_placeholder(self, index: int) -> str:
        return "%s"

    def render_json_extract(self, column_sql: str, path: str, as_text: bool) -> str:
        op = "->>" if as_text else "->"
        return f"{column_sql}{op}'{path}'"

    # -- 事务 ---------------------------------------------------------------

    async def begin(self) -> None:
        if self._tx_conn is not None:
            raise RuntimeError("Transaction already in progress")
        self._tx_conn = await self.pool.acquire()
        await self._tx_conn.begin()

    async def commit(self) -> None:
        if self._tx_conn is None:
            raise RuntimeError("No transaction in progress")
        try:
            await self._tx_conn.commit()
        finally:
            await self.pool.release(self._tx_conn)
            self._tx_conn = None

    async def rollback(self) -> None:
        if self._tx_conn is None:
            raise RuntimeError("No transaction in progress")
        try:
            await self._tx_conn.rollback()
        finally:
            await self.pool.release(self._tx_conn)
            self._tx_conn = None

    # -- 执行 ---------------------------------------------------------------

    async def execute(self, sql: str, params: list[Any]) -> Any:
        logger.debug("SQL: %s | params: %s", sql, params)
        if self._tx_conn is not None:
            async with self._tx_conn.cursor() as cursor:
                await cursor.execute(sql, params)
                return cursor.rowcount
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params)
                await conn.commit()
                return cursor.rowcount

    async def execute_insert(self, sql: str, params: list[Any]) -> Any:
        logger.debug("SQL: %s | params: %s", sql, params)
        if self._tx_conn is not None:
            async with self._tx_conn.cursor() as cursor:
                await cursor.execute(sql, params)
                return cursor.lastrowid
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params)
                await conn.commit()
                return cursor.lastrowid

    async def fetch(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        logger.debug("SQL: %s | params: %s", sql, params)
        if self._tx_conn is not None:
            async with self._tx_conn.cursor(asyncmy.cursors.DictCursor) as cursor:
                await cursor.execute(sql, params)
                return list(await cursor.fetchall())
        async with self.pool.acquire() as conn:
            async with conn.cursor(asyncmy.cursors.DictCursor) as cursor:
                await cursor.execute(sql, params)
                return list(await cursor.fetchall())

    async def fetchrow(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        logger.debug("SQL: %s | params: %s", sql, params)
        if self._tx_conn is not None:
            async with self._tx_conn.cursor(asyncmy.cursors.DictCursor) as cursor:
                await cursor.execute(sql, params)
                return cast(dict[str, Any] | None, await cursor.fetchone())
        async with self.pool.acquire() as conn:
            async with conn.cursor(asyncmy.cursors.DictCursor) as cursor:
                await cursor.execute(sql, params)
                return cast(dict[str, Any] | None, await cursor.fetchone())

    async def close(self) -> None:
        if self._tx_conn is not None:
            await self.pool.release(self._tx_conn)
            self._tx_conn = None
        if self._owns_pool:
            self.pool.close()
            await self.pool.wait_closed()

    # -- DDL ---------------------------------------------------------------

    def map_python_type(self, annotation: Any, column_info: ColumnInfo) -> str:
        if column_info.db_type is not None:
            return column_info.db_type

        base = resolve_base_type(annotation)

        if base is int:
            if column_info.auto_increment:
                return "INT AUTO_INCREMENT"
            return "INT"
        elif base is str:
            if column_info.max_length is not None:
                return f"VARCHAR({column_info.max_length})"
            return "TEXT"
        elif base is bool:
            return "BOOL"
        elif base is float:
            return "DOUBLE"
        elif base is bytes:
            return "BLOB"
        elif base is _dt.datetime:
            return "DATETIME"
        elif base is _dt.date:
            return "DATE"
        elif base is decimal.Decimal:
            return "DECIMAL(18,6)"
        elif base in (dict, list):
            return "JSON"
        return "TEXT"

    async def introspect_columns(self, table_name: str) -> dict[str, IntrospectedColumn]:
        query = (
            "SELECT COLUMN_NAME, COLUMN_TYPE, "
            "IS_NULLABLE, COLUMN_DEFAULT "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION"
        )
        rows = await self.fetch(query, [table_name])
        result: dict[str, IntrospectedColumn] = {}
        for row in rows:
            col = IntrospectedColumn(
                column_name=row["COLUMN_NAME"],
                data_type=row["COLUMN_TYPE"],
                is_nullable=row["IS_NULLABLE"] == "YES",
                column_default=row.get("COLUMN_DEFAULT"),
            )
            result[col.column_name.lower()] = col
        return result

    async def introspect_indexes(self, table_name: str) -> set[str]:
        query = (
            "SELECT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "AND INDEX_NAME != 'PRIMARY'"
        )
        rows = await self.fetch(query, [table_name])
        return {r["INDEX_NAME"] for r in rows}

    def build_modify_columns(
        self,
        model: type[Any],
        field_pairs: list[tuple[str, IntrospectedColumn]],
    ) -> str:
        """MySQL 风格：``ALTER TABLE ... MODIFY COLUMN col def, MODIFY COLUMN col def``。"""
        table = self.quote_identifier(model.__table__)
        col_defs: list[str] = []
        model_columns = model.__columns__
        model_column_info = model.__column_info__

        for field_name, existing in field_pairs:
            column = model_columns[field_name]
            info = model_column_info[field_name]
            annotation = model.model_fields[field_name].annotation
            new_type = self.map_python_type(annotation, info)

            old_normalized = self._normalize_type(existing.data_type)
            new_normalized = new_type.lower()

            type_changed = old_normalized != new_normalized
            null_changed = info.nullable != existing.is_nullable
            default_changed = self._default_changed(model, field_name, existing)

            if not type_changed and not null_changed and not default_changed:
                continue

            col_def = self._build_column_def(model, field_name, column, info)
            col_defs.append(f"MODIFY COLUMN {col_def}")

        if not col_defs:
            return ""

        sep = ",\n  "
        return f"ALTER TABLE {table}\n  {sep.join(col_defs)}"

    def build_sync_alter(
        self,
        model: type[Any],
        add_fields: list[str],
        modify_pairs: list[tuple[str, IntrospectedColumn]],
    ) -> str:
        """MySQL 风格：ADD COLUMN 和 MODIFY COLUMN 合并为一条 ALTER TABLE。"""
        table = self.quote_identifier(model.__table__)
        clauses: list[str] = []
        model_columns = model.__columns__
        model_column_info = model.__column_info__

        # -- ADD COLUMN -------------------------------------------------
        for field_name in add_fields:
            column = model_columns[field_name]
            info = model_column_info[field_name]
            col_def = self._build_column_def(model, field_name, column, info)
            clauses.append(f"ADD COLUMN {col_def}")

        # -- MODIFY COLUMN ----------------------------------------------
        for field_name, existing in modify_pairs:
            column = model_columns[field_name]
            info = model_column_info[field_name]
            annotation = model.model_fields[field_name].annotation
            new_type = self.map_python_type(annotation, info)

            old_normalized = self._normalize_type(existing.data_type)
            new_normalized = new_type.lower()

            type_changed = old_normalized != new_normalized
            null_changed = info.nullable != existing.is_nullable
            default_changed = self._default_changed(model, field_name, existing)

            if not type_changed and not null_changed and not default_changed:
                continue

            col_def = self._build_column_def(model, field_name, column, info)
            clauses.append(f"MODIFY COLUMN {col_def}")

        if not clauses:
            return ""

        sep = ",\n  "
        return f"ALTER TABLE {table}\n  {sep.join(clauses)}"

    def _render_json_default_expr(self, value: Any) -> str:
        """MySQL 使用 ``JSON_OBJECT`` / ``JSON_ARRAY`` 原生函数。"""
        from eorm.dialects.base import _render_json_default_expr

        return _render_json_default_expr(value, "JSON_OBJECT", "JSON_ARRAY")

    def _render_default_clause(self, field_info: Any, sql_type: str) -> str:
        """MySQL JSON 默认值使用原生函数 + 括号包裹：``DEFAULT (JSON_OBJECT())``。"""
        from pydantic_core import PydanticUndefined

        default = field_info.default
        if default is PydanticUndefined and field_info.default_factory is not None:
            default = field_info.default_factory()
        if default is PydanticUndefined:
            return ""

        sql_type_upper = sql_type.upper()
        if "JSON" in sql_type_upper:
            return f"DEFAULT ({self._render_json_default_expr(default)})"

        if isinstance(default, str):
            escaped = default.replace("'", "''")
            return f"DEFAULT '{escaped}'"
        if isinstance(default, bool):
            return f"DEFAULT {1 if default else 0}"
        if isinstance(default, int | float):
            return f"DEFAULT {default}"
        if default is None:
            return "DEFAULT NULL"

        return ""


async def create_mysql_dialect(**kwargs: Any) -> MySQLDialect:
    pool = await asyncmy.create_pool(**kwargs)
    return MySQLDialect(pool)
