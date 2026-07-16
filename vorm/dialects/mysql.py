from __future__ import annotations

import datetime as _dt
import decimal
import enum
import logging
from typing import Any, cast

import asyncmy

from vorm.ddl import IntrospectedColumn, get_enum_values, resolve_base_type
from vorm.dialects.base import AbstractDialect
from vorm.fields import ColumnInfo

logger = logging.getLogger("vorm")


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

    def _normalize_type(self, db_type: str) -> str:
        """MySQL: 归一化类型名，处理显示宽度和别名。

        MySQL 的整型列可能带回显示宽度（如 ``bigint(20)``, ``int(11)``, ``tinyint(1)``），
        这些宽度在 MySQL 8.0+ 已废弃，不影响存储，比对时应忽略。
        """
        import re

        t = db_type.lower().strip()
        # 去掉整型显示宽度：bigint(20) → bigint, int(11) → int, tinyint(1) → tinyint
        t = re.sub(r"^(tinyint|smallint|mediumint|int|bigint)\(\d+\)$", r"\1", t)
        # 类型别名
        t = t.replace("integer", "int")
        t = t.replace("boolean", "bool")
        t = t.replace("tinyint", "bool")  # MySQL TINYINT(1) ≡ BOOL
        t = t.replace("double precision", "double")
        t = t.replace("character varying", "varchar")
        t = t.replace("timestamp without time zone", "timestamp")
        t = t.replace("timestamp with time zone", "timestamptz")
        return t

    def _normalize_column_default(self, raw: Any) -> str:
        """MySQL: 将 COLUMN_DEFAULT 归一化以便比对。

        MySQL 的 COLUMN_DEFAULT 可能是 str / int / float / None，
        表达式默认值可能带括号或不带，需要统一处理。
        """
        if raw is None:
            return ""
        # MySQL 可能返回数值类型
        if isinstance(raw, int | float):
            return str(raw)
        s = str(raw).strip()
        # 去掉表达式默认值外层括号，如 (json_object()) → json_object()
        if s.startswith("(") and s.endswith(")"):
            s = s[1:-1].strip()
        return s.lower()

    def _default_changed(
        self,
        model: Any,
        field_name: str,
        existing: IntrospectedColumn,
    ) -> bool:
        """MySQL: 使用 MySQL 特定的默认值归一化逻辑比对。"""
        if model.__column_info__[field_name].auto_increment:
            return False

        field_info = model.model_fields[field_name]
        info = model.__column_info__[field_name]
        sql_type = self.map_python_type(field_info.annotation, info)
        expected = self._render_default_clause(field_info, sql_type)

        # 从模型生成的 DEFAULT 子句中提取值并归一化
        expected_value = expected.removeprefix("DEFAULT ").strip() if expected else ""
        # 对表达式默认值去掉外层括号（MySQL 生成 DEFAULT (expr)）
        if expected_value.startswith("(") and expected_value.endswith(")"):
            expected_value = expected_value[1:-1].strip()
        expected_normalized = expected_value.lower()

        db_normalized = self._normalize_column_default(existing.column_default)

        # 双方都空 → 无变化
        if not expected_normalized and not db_normalized:
            return False

        # 模型有表达式默认值，但 MySQL 不把表达式存入 COLUMN_DEFAULT（返回 NULL）
        # 比如 DEFAULT (JSON_OBJECT()) → MySQL COLUMN_DEFAULT = NULL
        # 此时无法比对，视为未变化
        if expected_normalized and not db_normalized:
            return False

        # 模型无默认值，但 DB 有 → 视为变化（模型中移除了默认值）
        if not expected_normalized and db_normalized:
            return True

        if expected_normalized.upper() == "NULL" and existing.column_default is None:
            return False

        # 去掉引号比较内容
        def _strip_quotes(v: str) -> str:
            v = v.strip()
            if v.startswith("'") and v.endswith("'"):
                return v[1:-1]
            return v

        # 尝试数值比较（处理 0.0 vs 0.00000 精度差）
        try:
            exp_num = float(expected_normalized)
            db_num = float(db_normalized)
            return exp_num != db_num
        except (ValueError, TypeError):
            pass

        return _strip_quotes(expected_normalized) != _strip_quotes(db_normalized)

    def map_python_type(self, annotation: Any, column_info: ColumnInfo) -> str:
        if column_info.db_type is not None:
            t = column_info.db_type
            if column_info.auto_increment and "AUTO_INCREMENT" not in t.upper():
                t += " AUTO_INCREMENT"
            return t

        base = resolve_base_type(annotation)

        # 枚举检测：Python enum → MySQL ENUM('val1','val2',...)
        # IntEnum 不生成 ENUM（MySQL ENUM 只支持字符串值）
        if isinstance(base, type) and issubclass(base, enum.Enum):
            if issubclass(base, int):
                return "INT"
            values = get_enum_values(annotation)
            quoted = ",".join(f"'{v}'" for v in values)
            return f"ENUM({quoted})"

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

    def _build_column_def(
        self,
        model: type[Any],
        field_name: str,
        column: Any,
        info: ColumnInfo,
    ) -> str:
        """MySQL 风格：列定义末尾追加 ``COMMENT 'text'``（当 info.comment 非空时）。"""
        base_def = super()._build_column_def(model, field_name, column, info)
        if info.comment:
            escaped = info.comment.replace("'", "''")
            base_def += f" COMMENT '{escaped}'"
        return base_def

    async def introspect_columns(self, table_name: str) -> dict[str, IntrospectedColumn]:
        query = (
            "SELECT COLUMN_NAME, COLUMN_TYPE, "
            "IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT, EXTRA "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s "
            "ORDER BY ORDINAL_POSITION"
        )
        rows = await self.fetch(query, [table_name])
        result: dict[str, IntrospectedColumn] = {}
        for row in rows:
            raw_comment = row.get("COLUMN_COMMENT")
            col = IntrospectedColumn(
                column_name=row["COLUMN_NAME"],
                data_type=row["COLUMN_TYPE"],
                is_nullable=row["IS_NULLABLE"] == "YES",
                column_default=row.get("COLUMN_DEFAULT"),
                column_comment=raw_comment if raw_comment else None,
                is_auto_increment=(row.get("EXTRA") or "").lower() == "auto_increment",
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
            # AUTO_INCREMENT 不在 MySQL COLUMN_TYPE 里，去掉后单独比对
            new_normalized = new_type.lower().replace(" auto_increment", "")

            type_changed = old_normalized != new_normalized
            null_changed = info.nullable != existing.is_nullable
            default_changed = self._default_changed(model, field_name, existing)
            comment_changed = self._comment_changed(model, field_name, existing)
            auto_increment_changed = info.auto_increment != existing.is_auto_increment

            if (
                not type_changed
                and not null_changed
                and not default_changed
                and not comment_changed
                and not auto_increment_changed
            ):
                continue

            col_def = self._modify_column_def(model, field_name, column, info)
            col_defs.append(f"MODIFY COLUMN {col_def}")

        if not col_defs:
            return ""

        sep = ",\n  "
        return f"ALTER TABLE {table}\n  {sep.join(col_defs)}"

    def _modify_column_def(
        self,
        model: type[Any],
        field_name: str,
        column: Any,
        info: ColumnInfo,
    ) -> str:
        """生成 MODIFY COLUMN 用的列定义（最小原则：只包含必要的子句）。

        与 ``_build_column_def`` 不同：
        - 不包含 PRIMARY KEY（已有 PK，重新声明会导致 1068）
        - 不包含 UNIQUE（已有约束，无需重建）
        - 只包含 MySQL MODIFY 必需的：类型 + NULL/NOT NULL + DEFAULT + COMMENT
        """
        annotation = model.model_fields[field_name].annotation
        sql_type = self.map_python_type(annotation, info)
        # 确保 auto_increment 被追加到类型中（即使 db_type 显式指定）
        if info.auto_increment and "AUTO_INCREMENT" not in sql_type.upper():
            sql_type += " AUTO_INCREMENT"
        col_name = self.quote_identifier(column.column_name)

        parts = [col_name, sql_type]
        if not info.nullable:
            parts.append("NOT NULL")
        else:
            parts.append("NULL")

        # DEFAULT 子句（auto_increment 列由 DB 生成值，不设 DEFAULT）
        if not info.auto_increment:
            default_clause = self._render_default_clause(model.model_fields[field_name], sql_type)
            if default_clause:
                parts.append(default_clause)

        # COMMENT 子句
        if info.comment:
            escaped = info.comment.replace("'", "''")
            parts.append(f"COMMENT '{escaped}'")

        return " ".join(parts)

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
            # AUTO_INCREMENT 不在 MySQL COLUMN_TYPE 里，比对时去掉
            new_normalized = new_type.lower().replace(" auto_increment", "")

            type_changed = old_normalized != new_normalized
            null_changed = info.nullable != existing.is_nullable
            default_changed = self._default_changed(model, field_name, existing)
            comment_changed = self._comment_changed(model, field_name, existing)

            if (
                not type_changed
                and not null_changed
                and not default_changed
                and not comment_changed
            ):
                continue

            col_def = self._modify_column_def(model, field_name, column, info)
            clauses.append(f"MODIFY COLUMN {col_def}")

        if not clauses:
            return ""

        sep = ",\n  "
        return f"ALTER TABLE {table}\n  {sep.join(clauses)}"

    def _render_json_default_expr(self, value: Any) -> str:
        """MySQL 使用 ``JSON_OBJECT`` / ``JSON_ARRAY`` 原生函数。"""
        from vorm.dialects.base import _render_json_default_expr

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
