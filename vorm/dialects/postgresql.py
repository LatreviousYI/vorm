from __future__ import annotations

import datetime as _dt
import decimal
import enum
import logging
import re
from typing import TYPE_CHECKING, Any

import asyncpg

from vorm.ddl import IntrospectedColumn, get_enum_values, is_enum_type, resolve_base_type
from vorm.dialects.base import AbstractDialect
from vorm.fields import ColumnInfo

if TYPE_CHECKING:
    from vorm.model import Model

logger = logging.getLogger("vorm")


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
        logger.debug("SQL: %s | params: %s", sql, params)
        if self._tx_conn is not None:
            return await _run_and_parse(self._tx_conn, sql, params)
        async with self.pool.acquire() as conn:
            return await _run_and_parse(conn, sql, params)

    async def fetch(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        logger.debug("SQL: %s | params: %s", sql, params)
        if self._tx_conn is not None:
            rows = await self._tx_conn.fetch(sql, *params)
            return [dict(row) for row in rows]
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [dict(row) for row in rows]

    async def fetchrow(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        logger.debug("SQL: %s | params: %s", sql, params)
        if self._tx_conn is not None:
            row = await self._tx_conn.fetchrow(sql, *params)
            return dict(row) if row is not None else None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(sql, *params)
            return dict(row) if row is not None else None

    async def close(self) -> None:
        if self._tx_conn is not None:
            await self.pool.release(self._tx_conn)
            self._tx_conn = None
        if self._owns_pool:
            await self.pool.close()

    # -- DDL ---------------------------------------------------------------

    def _normalize_type(self, db_type: str) -> str:
        """PG: 归一化类型名，与 ``map_python_type`` 的输出对齐。

        PG 内省返回的类型名可能是缩写或内部名，需要和模型映射的类型统一。
        保留参数（如 ``varchar(100)``）用于长度变更检测。
        """
        import re

        t = db_type.lower().strip()
        # PG 整型缩写：int2 → smallint, int4 → integer, int8 → bigint
        t = t.replace("int8", "bigint")
        t = t.replace("int4", "integer")
        t = t.replace("int2", "smallint")
        # 布尔：PG 缩写 bool → boolean（用词边界避免 boolean→booleanean）
        t = re.sub(r"\bbool\b", "boolean", t)
        # 浮点：PG float8 = double precision
        t = t.replace("float8", "double precision")
        # 数值：PG 的 numeric 即 decimal
        t = t.replace("numeric", "decimal")
        # 文本
        t = t.replace("character varying", "varchar")
        # 日期时间
        t = t.replace("timestamp without time zone", "timestamp")
        t = t.replace("timestamp with time zone", "timestamptz")
        return t

    def _alter_column_type(self, sql_type: str) -> str:
        """SERIAL/BIGSERIAL 不是真实的 PG 类型，ALTER 时转为底层类型。"""
        upper = sql_type.upper()
        if upper == "SERIAL":
            return "INTEGER"
        if upper == "BIGSERIAL":
            return "BIGINT"
        return sql_type

    def column_changed(
        self,
        model: type[Model],
        field_name: str,
        existing: IntrospectedColumn,
    ) -> bool:
        """PG：在基类比对之外，auto_increment 主键尚未有序列默认值时也算需修改。"""
        if super().column_changed(model, field_name, existing):
            return True

        info = model.__column_info__[field_name]
        if not (info.auto_increment and info.primary_key):
            return False

        new_type = self.map_python_type(model.model_fields[field_name].annotation, info).upper()
        if new_type not in ("SERIAL", "BIGSERIAL"):
            return False

        # 已有 nextval 默认值 → 序列已就绪，无需处理
        return "nextval" not in (existing.column_default or "").lower()

    def map_python_type(self, annotation: Any, column_info: ColumnInfo) -> str:
        if column_info.db_type is not None:
            db = column_info.db_type.upper()
            # BIGINT + auto_increment → BIGSERIAL（否则 PG 不会自动生成值）
            if column_info.auto_increment and column_info.primary_key:
                if db in ("BIGINT", "INT8"):
                    return "BIGSERIAL"
                if db in ("INTEGER", "INT", "INT4"):
                    return "SERIAL"
            return column_info.db_type

        base = resolve_base_type(annotation)

        # 枚举检测：Python enum.Enum → PG 自定义枚举类型
        # IntEnum 跳过（PG 不支持整型枚举），让其 fall 到 INTEGER
        if isinstance(base, type) and issubclass(base, enum.Enum):
            if not issubclass(base, int):
                return base.__name__.lower()

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
            "CASE "
            "  WHEN character_maximum_length IS NOT NULL "
            "    THEN data_type || '(' || character_maximum_length || ')' "
            "  WHEN data_type IN ('numeric', 'decimal') "
            "       AND numeric_precision IS NOT NULL "
            "    THEN data_type || '(' || numeric_precision "
            "         || ',' || COALESCE(numeric_scale, 0) || ')' "
            "  WHEN data_type = 'USER-DEFINED' THEN udt_name "
            "  ELSE data_type "
            "END AS data_type, "
            "is_nullable, column_default "
            "FROM information_schema.columns "
            "WHERE table_catalog = current_database() AND table_name = $1 "
            "ORDER BY ordinal_position"
        )
        rows = await self.fetch(query, [table_name])

        # Fetch column comments from pg_catalog
        comment_query = (
            "SELECT a.attname AS column_name, "
            "pg_catalog.col_description(c.oid, a.attnum) AS column_comment "
            "FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid "
            "WHERE c.relname = $1 AND a.attnum > 0 AND NOT a.attisdropped"
        )
        comment_rows = await self.fetch(comment_query, [table_name])
        comment_map: dict[str, str | None] = {}
        for row in comment_rows:
            comment_map[row["column_name"]] = row.get("column_comment")

        result: dict[str, IntrospectedColumn] = {}
        for row in rows:
            col = IntrospectedColumn(
                column_name=row["column_name"],
                data_type=row["data_type"],
                is_nullable=row["is_nullable"] == "YES",
                column_default=row.get("column_default"),
                column_comment=comment_map.get(row["column_name"]),
            )
            result[col.column_name.lower()] = col
        return result

    async def introspect_enum_types(self) -> dict[str, set[str]]:
        """Query pg_catalog to discover all existing enum types and their values."""
        query = (
            "SELECT t.typname AS type_name, e.enumlabel AS enum_value "
            "FROM pg_catalog.pg_type t "
            "JOIN pg_catalog.pg_enum e ON t.oid = e.enumtypid "
            "ORDER BY t.typname, e.enumsortorder"
        )
        rows = await self.fetch(query, [])
        result: dict[str, set[str]] = {}
        for row in rows:
            result.setdefault(row["type_name"], set()).add(row["enum_value"])
        return result

    def build_pre_create(
        self,
        model: type[Model],
        existing_enums: dict[str, set[str]] | None = None,
    ) -> list[str]:
        """Generate CREATE TYPE ... AS ENUM for each unique enum type used by the model.

        Skips types that already exist in the database.
        """
        result: list[str] = []
        existing = existing_enums or {}
        seen: set[str] = set()

        for field_name, info in model.__column_info__.items():
            annotation = model.model_fields[field_name].annotation
            if not is_enum_type(annotation):
                continue
            base = resolve_base_type(annotation)
            type_name = base.__name__.lower()
            if type_name in seen or type_name in existing:
                continue
            seen.add(type_name)
            values = get_enum_values(annotation)
            escaped_vals = ", ".join(f"'{v}'" for v in values)
            quoted = self.quote_identifier(type_name)
            result.append(f"CREATE TYPE {quoted} AS ENUM ({escaped_vals})")

        return result

    def build_pre_alter(
        self,
        model: type[Model],
        existing_enums: dict[str, set[str]] | None = None,
    ) -> list[str]:
        """Generate ALTER TYPE ... ADD VALUE for new enum members, or CREATE TYPE
        for enum types that don't exist yet.
        """
        result: list[str] = []
        existing = existing_enums or {}

        for field_name, info in model.__column_info__.items():
            annotation = model.model_fields[field_name].annotation
            if not is_enum_type(annotation):
                continue
            base = resolve_base_type(annotation)
            type_name = base.__name__.lower()
            quoted = self.quote_identifier(type_name)
            current_values = set(str(v) for v in get_enum_values(annotation))

            if type_name not in existing:
                escaped_vals = ", ".join(f"'{v}'" for v in sorted(current_values))
                result.append(f"CREATE TYPE {quoted} AS ENUM ({escaped_vals})")
            else:
                db_values = existing[type_name]
                for val in sorted(current_values - db_values):
                    result.append(f"ALTER TYPE {quoted} ADD VALUE '{val}'")

        return result

    def build_post_create(self, model: type[Model]) -> list[str]:
        """Generate COMMENT ON COLUMN for all columns that have a comment."""
        result: list[str] = []
        tbl = self.quote_identifier(model.__table__)
        for field_name, info in model.__column_info__.items():
            if not info.comment:
                continue
            col = self.quote_identifier(model.__columns__[field_name].column_name)
            escaped = info.comment.replace("'", "''")
            result.append(f"COMMENT ON COLUMN {tbl}.{col} IS '{escaped}'")
        return result

    def build_post_alter(
        self,
        model: type[Model],
        modified: list[tuple[str, IntrospectedColumn]],
        add_fields: list[str] | None = None,
    ) -> list[str]:
        """为 auto_increment 主键创建序列 + 处理列注释变更。"""
        result: list[str] = []
        tbl = self.quote_identifier(model.__table__)

        # COMMENT ON COLUMN for newly added columns
        if add_fields:
            for field_name in add_fields:
                info = model.__column_info__[field_name]
                if info.comment:
                    col = self.quote_identifier(model.__columns__[field_name].column_name)
                    escaped = info.comment.replace("'", "''")
                    result.append(f"COMMENT ON COLUMN {tbl}.{col} IS '{escaped}'")

        # COMMENT ON COLUMN for modified columns (comment added/changed/removed)
        for field_name, existing in modified:
            if self._comment_changed(model, field_name, existing):
                info = model.__column_info__[field_name]
                col = self.quote_identifier(model.__columns__[field_name].column_name)
                if info.comment:
                    escaped = info.comment.replace("'", "''")
                    result.append(f"COMMENT ON COLUMN {tbl}.{col} IS '{escaped}'")
                else:
                    result.append(f"COMMENT ON COLUMN {tbl}.{col} IS NULL")

        # 为 auto_increment 主键创建序列 + SET DEFAULT（PG 升级 BIGINT → BIGSERIAL）
        for field_name, existing in modified:
            info = model.__column_info__[field_name]
            if not (info.auto_increment and info.primary_key):
                continue

            column = model.__columns__[field_name]
            new_type = self.map_python_type(
                model.model_fields[field_name].annotation,
                info,
            ).upper()

            if new_type not in ("SERIAL", "BIGSERIAL"):
                continue

            col = self.quote_identifier(column.column_name)
            seq_name = f"{model.__table__}_{column.column_name}_seq"
            seq_quoted = self.quote_identifier(seq_name)

            # 已有 nextval 默认值 → 跳过（幂等：序列已存在且默认值已设置）
            existing_default = (existing.column_default or "").lower()
            if "nextval" in existing_default:
                continue

            result.append(f"CREATE SEQUENCE IF NOT EXISTS {seq_quoted} OWNED BY {tbl}.{col}")
            result.append(f"ALTER TABLE {tbl} ALTER COLUMN {col} SET DEFAULT nextval('{seq_name}')")

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
