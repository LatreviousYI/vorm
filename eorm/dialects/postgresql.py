from __future__ import annotations

import datetime as _dt
import decimal
import re
from typing import TYPE_CHECKING, Any

import asyncpg

from eorm.ddl import IntrospectedColumn, resolve_base_type
from eorm.dialects.base import AbstractDialect
from eorm.fields import ColumnInfo

if TYPE_CHECKING:
    from eorm.model import Model


def _parse_rowcount(status: str) -> int:
    """从 asyncpg 状态字符串中提取行数。

    "DELETE 3" → 3,  "INSERT 0 1" → 1,  "UPDATE 2" → 2
    """
    parts = status.split()
    if len(parts) >= 2 and parts[-1].isdigit():
        return int(parts[-1])
    return 0


async def _run_and_parse(conn: asyncpg.Connection, sql: str, params: list[Any]) -> int:
    raw = await conn.execute(sql, *params)
    if isinstance(raw, str):
        return _parse_rowcount(raw)
    return int(raw) if raw is not None else 0


class PostgreSQLDialect(AbstractDialect):
    quote_char = '"'

    def __init__(self, pool: asyncpg.Pool, *, _owns_pool: bool = True) -> None:
        self.pool = pool
        self._tx_conn: asyncpg.Connection | None = None
        self._owns_pool = _owns_pool

    def render_placeholder(self, index: int) -> str:
        return f"${index}"

    def render_json_extract(self, column_sql: str, path: str, as_text: bool) -> str:
        """将 JSON 路径转为 PG 操作符链。

        ``$.key`` → ``->'key'``, ``$[0]`` → ``->0``, ``$.a.b`` → ``->'a'->>'b'``
        """
        parts = self._parse_json_path(path)
        if not parts:
            return column_sql

        result = column_sql
        for i, part in enumerate(parts):
            is_last = i == len(parts) - 1
            op = "->>" if (as_text and is_last) else "->"
            if isinstance(part, int):
                result += f"{op}{part}"
            else:
                escaped = part.replace("'", "''")
                result += f"{op}'{escaped}'"
        return result

    @staticmethod
    def _parse_json_path(path: str) -> list:
        """Parse a JSON path like ``$.key.sub[0].name`` into ``['key', 'sub', 0, 'name']``."""
        if not path.startswith("$"):
            return [path]

        result: list = []
        # Match .key or [index]
        tokens = re.findall(r"\.(\w+)|\[(\d+)\]", path)
        for dot_match, bracket_match in tokens:
            if bracket_match:
                result.append(int(bracket_match))
            elif dot_match:
                result.append(dot_match)
        return result

    # -- 事务 ---------------------------------------------------------------

    async def begin(self) -> None:
        if self._tx_conn is not None:
            raise RuntimeError("Transaction already in progress")
        self._tx_conn = await self.pool.acquire()
        await self._tx_conn.execute("BEGIN")

    async def commit(self) -> None:
        conn = self._tx_conn
        if conn is None:
            raise RuntimeError("No transaction in progress")
        try:
            await conn.execute("COMMIT")
        finally:
            self._tx_conn = None
            await self.pool.release(conn)

    async def rollback(self) -> None:
        conn = self._tx_conn
        if conn is None:
            raise RuntimeError("No transaction in progress")
        try:
            await conn.execute("ROLLBACK")
        finally:
            self._tx_conn = None
            await self.pool.release(conn)

    # -- 执行 ---------------------------------------------------------------

    @staticmethod
    def _is_connection_error(exc: Exception) -> bool:
        """PostgreSQL 连接断开类错误。"""
        try:
            import asyncpg.exceptions as pg_exc
        except ImportError:
            return False
        return isinstance(
            exc,
            pg_exc.ConnectionDoesNotExistError
            | pg_exc.ConnectionFailureError
            | pg_exc.InterfaceError,
        )

    def build_insert(self, instance: Model) -> tuple[str, list[Any]]:
        sql, params = super().build_insert(instance)
        pk_col = type(instance).primary_key_column()
        sql = f"{sql} RETURNING {self.quote_identifier(pk_col.column_name)}"
        return sql, params

    async def execute_insert(self, sql: str, params: list[Any]) -> Any:
        """Execute INSERT ... RETURNING and return the generated PK value."""
        row = await self.fetchrow(sql, params)
        if row is not None:
            return next(iter(row.values()), None)
        return None

    async def execute(self, sql: str, params: list[Any]) -> Any:
        """执行写操作，INSERT 返回 lastrowid，其他返回受影响行数。"""

        async def _do() -> Any:
            if self._tx_conn is not None:
                return await _run_and_parse(self._tx_conn, sql, params)
            else:
                async with self.pool.acquire() as conn:
                    return await _run_and_parse(conn, sql, params)

        return await self._retry_on_connection_error(_do)

    async def fetch(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        async def _do() -> list[dict[str, Any]]:
            if self._tx_conn is not None:
                rows = await self._tx_conn.fetch(sql, *params)
                return [dict(row) for row in rows]
            else:
                async with self.pool.acquire() as conn:
                    rows = await conn.fetch(sql, *params)
                    return [dict(row) for row in rows]

        return await self._retry_on_connection_error(_do)

    async def fetchrow(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        async def _do() -> dict[str, Any] | None:
            if self._tx_conn is not None:
                row = await self._tx_conn.fetchrow(sql, *params)
                return dict(row) if row is not None else None
            else:
                async with self.pool.acquire() as conn:
                    row = await conn.fetchrow(sql, *params)
                    return dict(row) if row is not None else None

        return await self._retry_on_connection_error(_do)

    async def close(self) -> None:
        if self._tx_conn is not None:
            await self.pool.release(self._tx_conn)
            self._tx_conn = None
        if self._owns_pool:
            await self.pool.close()

    # -- DDL ---------------------------------------------------------------

    def map_python_type(self, annotation: Any, column_info: ColumnInfo) -> str:
        if column_info.db_type is not None:
            return column_info.db_type

        base = resolve_base_type(annotation)

        if base is int:
            if column_info.auto_increment and column_info.primary_key:
                return "SERIAL"
            return "INTEGER"
        elif base is str:
            if column_info.max_length is not None:
                return f"VARCHAR({column_info.max_length})"
            return "TEXT"
        elif base is bool:
            return "BOOLEAN"
        elif base is float:
            return "DOUBLE PRECISION"
        elif base is bytes:
            return "BYTEA"
        elif base is _dt.datetime:
            return "TIMESTAMP"
        elif base is _dt.date:
            return "DATE"
        elif base is decimal.Decimal:
            return "DECIMAL(18,6)"
        elif base in (dict, list):
            return "JSONB"
        return "TEXT"

    async def introspect_columns(self, table_name: str) -> dict[str, IntrospectedColumn]:
        query = (
            "SELECT column_name, "
            "CASE WHEN character_maximum_length IS NOT NULL "
            "  THEN data_type || '(' || character_maximum_length || ')' "
            "  ELSE data_type END AS data_type, "
            "is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_catalog = current_database() AND table_name = $1 "
            "ORDER BY ordinal_position"
        )
        rows = await self.fetch(query, [table_name])
        result: dict[str, IntrospectedColumn] = {}
        for row in rows:
            col = IntrospectedColumn(
                column_name=row["column_name"],
                data_type=row["data_type"],
                is_nullable=row["is_nullable"] == "YES",
                column_default=row.get("column_default"),
            )
            result[col.column_name.lower()] = col
        return result

    async def introspect_indexes(self, table_name: str) -> set[str]:
        query = "SELECT indexname FROM pg_indexes WHERE tablename = $1"
        rows = await self.fetch(query, [table_name])
        names = {r["indexname"] for r in rows}
        # PG 自动为主键创建的索引（通常叫 <table>_pkey）
        names.discard(f"{table_name}_pkey")
        return names


async def create_postgresql_dialect(**kwargs: Any) -> PostgreSQLDialect:
    pool = await asyncpg.create_pool(**kwargs)
    return PostgreSQLDialect(pool)
