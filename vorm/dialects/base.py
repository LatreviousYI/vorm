from __future__ import annotations

import datetime as _dt
import enum
import json as _json_mod
import re
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from vorm.fields import ColumnInfo

if TYPE_CHECKING:
    from vorm.ddl import IntrospectedColumn
    from vorm.model import Model
    from vorm.query import QuerySet


# ------------------------------------------------------------------
# JSON 默认值 → 数据库原生函数
# ------------------------------------------------------------------


def _render_json_scalar(value: Any, obj_func: str, arr_func: str) -> str:
    """将 Python 标量转为 SQL 字面量，dict/list 递归渲染。"""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("'", "''")
        return f"'{escaped}'"
    if isinstance(value, dict):
        return _render_json_default_expr(value, obj_func, arr_func)
    if isinstance(value, list):
        return _render_json_default_expr(value, obj_func, arr_func)
    return f"'{value}'"


def _render_json_default_expr(value: Any, obj_func: str, arr_func: str) -> str:
    """将 Python dict/list 转为数据库原生 JSON 函数调用表达式。

    不依赖 ``json.dumps()``，直接用 ``JSON_OBJECT()`` / ``JSON_ARRAY()``
    等数据库内置函数生成 DEFAULT 子句，符合主流 DDL 实践。
    """
    if isinstance(value, dict):
        if not value:
            return f"{obj_func}()"
        items: list[str] = []
        for k, v in value.items():
            key_str = str(k).replace("'", "''")
            items.append(f"'{key_str}', {_render_json_scalar(v, obj_func, arr_func)}")
        return f"{obj_func}({', '.join(items)})"
    if isinstance(value, list):
        if not value:
            return f"{arr_func}()"
        items = [_render_json_scalar(v, obj_func, arr_func) for v in value]
        return f"{arr_func}({', '.join(items)})"
    # 非容器类型回退（理论上不会走到这里）
    return _render_json_scalar(value, obj_func, arr_func)


class AbstractDialect(ABC):
    quote_char = '"'

    # ------------------------------------------------------------------
    # 标识符 & 占位符
    # ------------------------------------------------------------------

    def quote_identifier(self, name: str) -> str:
        escaped = name.replace(self.quote_char, self.quote_char * 2)
        return f"{self.quote_char}{escaped}{self.quote_char}"

    @abstractmethod
    def render_placeholder(self, index: int) -> str:
        raise NotImplementedError

    @abstractmethod
    def render_json_extract(self, column_sql: str, path: str, as_text: bool) -> str:
        """生成 JSON 路径提取 SQL 片段。

        :param column_sql: 已转义的列引用，如 ``"users"."data"``
        :param path: JSON 路径，如 ``"$.key"``
        :param as_text: ``True`` → ``->>`` 操作符（返回文本），``False`` → ``->``
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 事务 & 健康检查
    # ------------------------------------------------------------------

    @abstractmethod
    async def begin(self) -> None:
        """开启事务。"""
        raise NotImplementedError

    @abstractmethod
    async def commit(self) -> None:
        """提交事务。"""
        raise NotImplementedError

    @abstractmethod
    async def rollback(self) -> None:
        """回滚事务。"""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    @abstractmethod
    async def execute(self, sql: str, params: list[Any]) -> Any:
        raise NotImplementedError

    @abstractmethod
    async def execute_insert(self, sql: str, params: list[Any]) -> Any:
        """Execute an INSERT and return the generated primary key value."""
        raise NotImplementedError

    @abstractmethod
    async def fetch(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def fetchrow(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

    async def ping(self) -> bool:
        """健康检查：执行 ``SELECT 1`` 验证连接可用。"""
        try:
            row = await self.fetchrow("SELECT 1 AS ok", [])
            return row is not None and row.get("ok") == 1
        except Exception:
            return False

    # ------------------------------------------------------------------
    # SQL 生成
    # ------------------------------------------------------------------

    def build_select(self, query: QuerySet[Any], *, count: bool = False) -> tuple[str, list[Any]]:
        params: list[Any] = []
        model = query.model
        table = self.quote_identifier(model.__table__)

        if count:
            sql = f"SELECT COUNT(*) AS count FROM {table}"
            for j in query.joins:
                jt = self.quote_identifier(j.model.__table__)
                on_clause = j.on.render(self, params)
                sql = f"{sql} {j.join_type} JOIN {jt} ON {on_clause}"
        elif query._selected is not None:
            # 用户指定了列（支持别名）
            column_sql = ", ".join(col.render(self) for col in query._selected)
            sql = f"SELECT {column_sql} FROM {table}"
            for j in query.joins:
                jt = self.quote_identifier(j.model.__table__)
                on_clause = j.on.render(self, params)
                sql = f"{sql} {j.join_type} JOIN {jt} ON {on_clause}"
        elif query.joins:
            # 多表 JOIN：列名输出为 表名.列名 AS 表名_列名
            all_tables = [(model.__table__, model.__columns__)]
            for j in query.joins:
                all_tables.append((j.model.__table__, j.model.__columns__))

            column_parts: list[str] = []
            for tname, tcolumns in all_tables:
                for col in tcolumns.values():
                    col_sql = col.render(self)  # "users"."id"
                    alias = f"{tname}_{col.column_name}"
                    column_parts.append(f"{col_sql} AS {self.quote_identifier(alias)}")

            sql = f"SELECT {', '.join(column_parts)} FROM {table}"
            for j in query.joins:
                jt = self.quote_identifier(j.model.__table__)
                on_clause = j.on.render(self, params)
                sql = f"{sql} {j.join_type} JOIN {jt} ON {on_clause}"
        else:
            columns = ", ".join(col.render(self) for col in model.__columns__.values())
            sql = f"SELECT {columns} FROM {table}"

        if query.filters:
            where = " AND ".join(expr.render(self, params) for expr in query.filters)
            sql = f"{sql} WHERE {where}"
        if query.orderings and not count:
            order_by = ", ".join(order.render(self) for order in query.orderings)
            sql = f"{sql} ORDER BY {order_by}"
        if query.limit_count is not None and not count:
            params.append(query.limit_count)
            sql = f"{sql} LIMIT {self.render_placeholder(len(params))}"
        if query.offset_count is not None and not count:
            params.append(query.offset_count)
            sql = f"{sql} OFFSET {self.render_placeholder(len(params))}"
        return sql, params

    def build_insert(self, instance: Model) -> tuple[str, list[Any]]:
        model = type(instance)
        field_names, params = self._collect_writable_fields(instance)
        model_columns = model.__columns__
        columns = ", ".join(
            self.quote_identifier(model_columns[fn].column_name) for fn in field_names
        )
        placeholders = ", ".join(self.render_placeholder(i) for i in range(1, len(params) + 1))
        sql = (
            f"INSERT INTO {self.quote_identifier(model.__table__)} "
            f"({columns}) VALUES ({placeholders})"
        )
        return sql, params

    def build_bulk_insert(self, instances: list[Model]) -> tuple[str, list[Any]]:
        """生成一条 INSERT INTO ... VALUES (...), (...), ... 语句。"""
        if not instances:
            raise ValueError("bulk_insert requires at least one instance")
        model = type(instances[0])
        # 用第一条实例确定列（假设所有实例列结构相同）
        field_names, _ = self._collect_writable_fields(instances[0])
        model_columns = model.__columns__
        column_names = [self.quote_identifier(model_columns[fn].column_name) for fn in field_names]

        params: list[Any] = []
        row_placeholders: list[str] = []
        for instance in instances:
            row_params: list[Any] = []
            for fn in field_names:
                val = getattr(instance, fn)
                if isinstance(val, dict | list):
                    val = _json_mod.dumps(val)
                row_params.append(val)
            params.extend(row_params)
            row_ph = ", ".join(
                self.render_placeholder(i)
                for i in range(len(params) - len(row_params) + 1, len(params) + 1)
            )
            row_placeholders.append(f"({row_ph})")

        sql = (
            f"INSERT INTO {self.quote_identifier(model.__table__)} "
            f"({', '.join(column_names)}) VALUES {', '.join(row_placeholders)}"
        )
        return sql, params

    def build_update(self, instance: Model) -> tuple[str, list[Any]]:
        model = type(instance)
        pk_name = model.primary_key_name()
        params: list[Any] = []
        assignments: list[str] = []
        model_column_info = model.__column_info__
        for field_name, column in model.__columns__.items():
            if field_name == pk_name:
                continue

            info = model_column_info[field_name]
            # "create" 行为的时间戳字段在 UPDATE 时跳过，保留原值
            if info.timestamp_behavior == "create":
                continue
            # "both" / "update" 行为在 UPDATE 时重新计算
            if info.timestamp_behavior in ("both", "update"):
                factory = model.model_fields[field_name].default_factory
                if factory is not None:
                    value = factory()
                else:
                    value = _dt.datetime.now()
            else:
                value = getattr(instance, field_name)

            if isinstance(value, dict | list):
                value = _json_mod.dumps(value)
            params.append(value)
            col_sql = self.quote_identifier(column.column_name)
            placeholder = self.render_placeholder(len(params))
            assignments.append(f"{col_sql} = {placeholder}")

        params.append(instance.primary_key_value())
        pk_col = model.primary_key_column()
        where = f"{pk_col.render(self)} = {self.render_placeholder(len(params))}"
        sql = (
            f"UPDATE {self.quote_identifier(model.__table__)} "
            f"SET {', '.join(assignments)} WHERE {where}"
        )
        return sql, params

    def build_update_by_query(
        self, query: QuerySet[Any], values: dict[str, Any]
    ) -> tuple[str, list[Any]]:
        """生成 UPDATE ... SET ... WHERE ... 语句（按条件批量更新）。

        自动为 ``timestamp_behavior`` 为 ``"both"`` 或 ``"update"`` 的字段注入当前时间，
        除非用户已在 ``values`` 中显式传入。
        """
        params: list[Any] = []
        model = query.model
        table = self.quote_identifier(model.__table__)

        # 自动注入时间戳字段（"both" / "update"），用户显式值优先
        all_values = dict(values)
        for field_name, info in model.__column_info__.items():
            if info.timestamp_behavior in ("both", "update") and field_name not in all_values:
                factory = model.model_fields[field_name].default_factory
                all_values[field_name] = factory() if factory is not None else _dt.datetime.now()

        assignments: list[str] = []
        model_columns = model.__columns__
        for field_name, value in all_values.items():
            col = self.quote_identifier(model_columns[field_name].column_name)
            if isinstance(value, dict | list):
                value = _json_mod.dumps(value)
            params.append(value)
            assignments.append(f"{col} = {self.render_placeholder(len(params))}")
        sql = f"UPDATE {table} SET {', '.join(assignments)}"
        if query.filters:
            where = " AND ".join(expr.render(self, params) for expr in query.filters)
            sql = f"{sql} WHERE {where}"
        return sql, params

    def build_delete(self, instance: Model) -> tuple[str, list[Any]]:
        model = type(instance)
        pk_col = model.primary_key_column()
        params = [instance.primary_key_value()]
        sql = (
            f"DELETE FROM {self.quote_identifier(model.__table__)} "
            f"WHERE {pk_col.render(self)} = {self.render_placeholder(1)}"
        )
        return sql, params

    def build_delete_by_query(self, query: QuerySet[Any]) -> tuple[str, list[Any]]:
        """生成 DELETE FROM ... WHERE ... 语句（按条件批量删除）。"""
        params: list[Any] = []
        model = query.model
        table = self.quote_identifier(model.__table__)
        sql = f"DELETE FROM {table}"
        if query.filters:
            where = " AND ".join(expr.render(self, params) for expr in query.filters)
            sql = f"{sql} WHERE {where}"
        return sql, params

    # ------------------------------------------------------------------
    # DDL —— 建表 & 同步
    # ------------------------------------------------------------------

    @abstractmethod
    def map_python_type(self, annotation: Any, column_info: ColumnInfo) -> str:
        """将 Python 类型注解映射为方言特定的 SQL 类型字符串。

        各方言必须实现此方法，根据注解和 ColumnInfo（max_length、auto_increment、
        db_type 等）返回正确的 SQL 类型。
        """
        raise NotImplementedError

    @abstractmethod
    async def introspect_columns(self, table_name: str) -> dict[str, IntrospectedColumn]:
        """查询 INFORMATION_SCHEMA 返回表中已有列。

        如果表不存在，返回空 dict。
        """
        raise NotImplementedError

    def _build_column_def(
        self,
        model: type[Model],
        field_name: str,
        column: Any,
        info: ColumnInfo,
    ) -> str:
        """构建单列定义，如 ``"id" INT AUTO_INCREMENT PRIMARY KEY``。"""
        annotation = model.model_fields[field_name].annotation
        sql_type = self.map_python_type(annotation, info)
        col_name = self.quote_identifier(column.column_name)

        parts = [col_name, sql_type]
        if info.primary_key:
            parts.append("PRIMARY KEY")
        if not info.nullable:
            parts.append("NOT NULL")
        if info.unique:
            parts.append("UNIQUE")

        # Pydantic-level default → DDL DEFAULT clause
        # auto-increment 列由数据库生成值，跳过 DEFAULT 子句（MySQL 会拒绝
        # ``NOT NULL DEFAULT NULL``，PG 的 SERIAL 也不需要）
        if not info.auto_increment:
            default_clause = self._render_default_clause(model.model_fields[field_name], sql_type)
            if default_clause:
                parts.append(default_clause)

        return " ".join(parts)

    def _render_json_default_expr(self, value: Any) -> str:
        """将 Python dict/list 转为数据库原生 JSON 函数调用表达式。

        默认使用 PostgreSQL 的 ``jsonb_build_object`` / ``jsonb_build_array``。
        MySQL 方言需覆盖为 ``JSON_OBJECT`` / ``JSON_ARRAY``。
        """
        return _render_json_default_expr(value, "jsonb_build_object", "jsonb_build_array")

    def _render_default_clause(self, field_info: Any, sql_type: str) -> str:
        """生成 DEFAULT 子句（PG 风格）。

        各方言可覆盖以适配不同语法（MySQL JSON 需要括号包裹等）。
        """
        from pydantic_core import PydanticUndefined

        default = field_info.default
        if default is PydanticUndefined and field_info.default_factory is not None:
            default = field_info.default_factory()
        if default is PydanticUndefined:
            return ""

        sql_type_upper = sql_type.upper()
        if "JSON" in sql_type_upper or "JSONB" in sql_type_upper:
            return f"DEFAULT {self._render_json_default_expr(default)}"

        if isinstance(default, enum.Enum):
            val = default.value
            if isinstance(val, str):
                escaped = val.replace("'", "''")
                return f"DEFAULT '{escaped}'"
            return f"DEFAULT {val}"
        if isinstance(default, str):
            escaped = default.replace("'", "''")
            return f"DEFAULT '{escaped}'"
        if isinstance(default, bool):
            return f"DEFAULT {'TRUE' if default else 'FALSE'}"
        if isinstance(default, int | float):
            return f"DEFAULT {default}"
        if default is None:
            return "DEFAULT NULL"

        return ""

    # ------------------------------------------------------------------
    # 默认值比较（sync_table 用）
    # ------------------------------------------------------------------

    def _normalize_column_default(self, raw: str | None) -> str:
        """Normalize database column_default for comparison.

        PG-style: strips ``::type`` suffix (e.g. ``'hello'::character varying`` → ``'hello'``).
        MySQL dialects override this to pass through raw values.
        """
        if raw is None:
            return ""
        # Strip ::type suffix like ::character varying, ::text, ::jsonb, ::boolean, etc.
        normalized = re.sub(r"::\w+(\s+\w+)*\s*$", "", raw.strip())
        return normalized.strip()

    def _alter_column_type(self, sql_type: str) -> str:
        """Return the type to use in ALTER COLUMN ... TYPE clause.

        PG overrides this to convert SERIAL/BIGSERIAL to their underlying types.
        """
        return sql_type

    def _default_changed(
        self,
        model: type[Model],
        field_name: str,
        existing: IntrospectedColumn,
    ) -> bool:
        """Check if the DEFAULT value has changed between model and DB."""
        # auto_increment 列的值由数据库生成，DEFAULT 无意义，无需比较
        if model.__column_info__[field_name].auto_increment:
            return False

        field_info = model.model_fields[field_name]
        info = model.__column_info__[field_name]
        annotation = field_info.annotation
        sql_type = self.map_python_type(annotation, info)
        expected = self._render_default_clause(field_info, sql_type)

        # "DEFAULT 'hello'" → "'hello'"
        expected_value = expected.removeprefix("DEFAULT ").strip() if expected else ""
        db_value = self._normalize_column_default(existing.column_default)

        # Both empty → no change
        if not expected_value and not db_value:
            return False

        # "DEFAULT NULL" ↔ DB NULL 语义等价（DB 无法区分"无默认值"和"DEFAULT NULL"）
        if expected_value.upper() == "NULL" and existing.column_default is None:
            return False

        # One has default, the other doesn't
        if bool(expected_value) != bool(db_value):
            return True

        # Both non-empty → normalize and compare (case-insensitive)
        def _strip_quotes(v: str) -> str:
            v = v.strip()
            if v.startswith("'") and v.endswith("'"):
                return v[1:-1]
            return v

        return _strip_quotes(expected_value).upper() != _strip_quotes(db_value).upper()

    def _comment_changed(
        self,
        model: type[Model],
        field_name: str,
        existing: IntrospectedColumn,
    ) -> bool:
        """Check if the column comment has changed between model and DB."""
        info = model.__column_info__[field_name]
        expected = (info.comment or "").strip()
        db_comment = (existing.column_comment or "").strip()
        return expected != db_comment

    def column_changed(
        self,
        model: type[Model],
        field_name: str,
        existing: IntrospectedColumn,
    ) -> bool:
        """Return ``True`` if this column needs any ALTER / post-alter DDL.

        作为 ``sync_table`` 判断"哪些列真正需要修改"的单一真相源。注释变更也计入
        （PG 通过 ``build_post_alter`` 处理 ``COMMENT ON COLUMN``，不属于类型/默认值比对）。
        """
        info = model.__column_info__[field_name]
        annotation = model.model_fields[field_name].annotation
        new_type = self.map_python_type(annotation, info)

        type_changed = (
            self._normalize_type(existing.data_type) != self._alter_column_type(new_type).lower()
        )
        null_changed = info.nullable != existing.is_nullable
        default_changed = self._default_changed(model, field_name, existing)
        comment_changed = self._comment_changed(model, field_name, existing)
        return type_changed or null_changed or default_changed or comment_changed

    def build_create_table(self, model: type[Model]) -> str:
        """生成 ``CREATE TABLE IF NOT EXISTS ...`` 语句。"""
        table = self.quote_identifier(model.__table__)
        definitions: list[str] = []
        model_column_info = model.__column_info__
        for field_name, column in model.__columns__.items():
            info = model_column_info[field_name]
            definitions.append(f"  {self._build_column_def(model, field_name, column, info)}")
        columns_sql = ",\n".join(definitions)
        return f"CREATE TABLE IF NOT EXISTS {table} (\n{columns_sql}\n)"

    def build_pre_create(
        self,
        model: type[Model],
        existing_enums: dict[str, set[str]] | None = None,
    ) -> list[str]:
        """Return DDL to execute BEFORE CREATE TABLE (e.g., CREATE TYPE for PG enums).

        The base returns an empty list. PostgreSQL overrides this.
        """
        return []

    def build_pre_alter(
        self,
        model: type[Model],
        existing_enums: dict[str, set[str]] | None = None,
    ) -> list[str]:
        """Return DDL to execute BEFORE ALTER TABLE (e.g., ALTER TYPE ADD VALUE for PG).

        The base returns an empty list. PostgreSQL overrides this.
        """
        return []

    def build_post_create(self, model: type[Model]) -> list[str]:
        """Return extra DDL to execute after CREATE TABLE (e.g. COMMENT ON COLUMN for PG).

        The base returns an empty list. PostgreSQL overrides this to generate
        ``COMMENT ON COLUMN`` statements.
        """
        return []

    def build_add_column(self, model: type[Model], field_name: str) -> str:
        """生成 ``ALTER TABLE ... ADD COLUMN ...`` 语句（单列）。"""
        column = model.__columns__[field_name]
        info = model.__column_info__[field_name]
        col_def = self._build_column_def(model, field_name, column, info)
        table = self.quote_identifier(model.__table__)
        return f"ALTER TABLE {table} ADD COLUMN {col_def}"

    def build_add_columns(self, model: type[Model], field_names: list[str]) -> str:
        """生成 ``ALTER TABLE ... ADD COLUMN ..., ADD COLUMN ...`` 语句（多列合并）。

        所有新增列合并在一条 ALTER TABLE 中执行，减少网络往返。
        """
        table = self.quote_identifier(model.__table__)
        col_defs: list[str] = []
        model_columns = model.__columns__
        model_column_info = model.__column_info__
        for field_name in field_names:
            column = model_columns[field_name]
            info = model_column_info[field_name]
            col_def = self._build_column_def(model, field_name, column, info)
            col_defs.append(f"ADD COLUMN {col_def}")
        sep = ",\n  "
        return f"ALTER TABLE {table}\n  {sep.join(col_defs)}"

    # -- 列修改（PG 风格：ALTER COLUMN ... TYPE / SET NOT NULL） ---------

    def _normalize_type(self, db_type: str) -> str:
        """将数据库返回的类型名规范化为可比较形式（小写 + 别名统一）。"""
        t = db_type.lower().strip()
        t = t.replace("character varying", "varchar")
        t = t.replace("double precision", "double")
        t = t.replace("timestamp without time zone", "timestamp")
        t = t.replace("timestamp with time zone", "timestamptz")
        return t

    def build_modify_columns(
        self,
        model: type[Model],
        field_pairs: list[tuple[str, IntrospectedColumn]],
    ) -> str:
        """生成 ``ALTER TABLE ... ALTER COLUMN ...`` 语句（多列类型/约束修改）。

        默认 PG 风格——MySQL 方言需要覆盖为 ``MODIFY COLUMN`` 语法。
        """
        table = self.quote_identifier(model.__table__)
        clauses: list[str] = []
        model_columns = model.__columns__
        model_column_info = model.__column_info__

        for field_name, existing in field_pairs:
            column = model_columns[field_name]
            info = model_column_info[field_name]
            annotation = model.model_fields[field_name].annotation
            new_type = self.map_python_type(annotation, info)
            col_name = self.quote_identifier(column.column_name)

            old_normalized = self._normalize_type(existing.data_type)
            # SERIAL/BIGSERIAL 在 PG 内部是 INTEGER/BIGINT + nextval()，
            # 比对时也转为底层类型，否则每次 sync 都误判为 type_change
            new_normalized = self._alter_column_type(new_type).lower()

            type_changed = old_normalized != new_normalized
            null_changed = info.nullable != existing.is_nullable
            default_changed = self._default_changed(model, field_name, existing)

            if not type_changed and not null_changed and not default_changed:
                continue

            if type_changed:
                clauses.append(f"ALTER COLUMN {col_name} TYPE {self._alter_column_type(new_type)}")

            if null_changed:
                if info.nullable:
                    clauses.append(f"ALTER COLUMN {col_name} DROP NOT NULL")
                else:
                    clauses.append(f"ALTER COLUMN {col_name} SET NOT NULL")

            if default_changed:
                expected = self._render_default_clause(
                    model.model_fields[field_name],
                    new_type,
                )
                if expected:
                    clauses.append(f"ALTER COLUMN {col_name} SET {expected}")
                else:
                    clauses.append(f"ALTER COLUMN {col_name} DROP DEFAULT")

        if not clauses:
            return ""

        sep = ",\n  "
        return f"ALTER TABLE {table}\n  {sep.join(clauses)}"

    def build_sync_alter(
        self,
        model: type[Model],
        add_fields: list[str],
        modify_pairs: list[tuple[str, IntrospectedColumn]],
    ) -> str:
        """生成合并 ADD COLUMN 和 MODIFY 的单个 ALTER TABLE 语句（PG 风格）。

        MySQL 方言需覆盖为 ``MODIFY COLUMN`` 语法。
        """
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

        # -- MODIFY -----------------------------------------------------
        for field_name, existing in modify_pairs:
            column = model_columns[field_name]
            info = model_column_info[field_name]
            annotation = model.model_fields[field_name].annotation
            new_type = self.map_python_type(annotation, info)
            col_name = self.quote_identifier(column.column_name)

            old_normalized = self._normalize_type(existing.data_type)
            # SERIAL/BIGSERIAL 在 PG 内部是 INTEGER/BIGINT + nextval()，
            # 比对时也转为底层类型，否则每次 sync 都误判为 type_change
            new_normalized = self._alter_column_type(new_type).lower()

            type_changed = old_normalized != new_normalized
            null_changed = info.nullable != existing.is_nullable
            default_changed = self._default_changed(model, field_name, existing)

            if not type_changed and not null_changed and not default_changed:
                continue

            if type_changed:
                clauses.append(f"ALTER COLUMN {col_name} TYPE {self._alter_column_type(new_type)}")
            if null_changed:
                if info.nullable:
                    clauses.append(f"ALTER COLUMN {col_name} DROP NOT NULL")
                else:
                    clauses.append(f"ALTER COLUMN {col_name} SET NOT NULL")
            if default_changed:
                expected = self._render_default_clause(
                    model.model_fields[field_name],
                    new_type,
                )
                if expected:
                    clauses.append(f"ALTER COLUMN {col_name} SET {expected}")
                else:
                    clauses.append(f"ALTER COLUMN {col_name} DROP DEFAULT")

        if not clauses:
            return ""

        sep = ",\n  "
        return f"ALTER TABLE {table}\n  {sep.join(clauses)}"

    def build_post_alter(
        self,
        model: type[Model],
        modified: list[tuple[str, IntrospectedColumn]],
        add_fields: list[str] | None = None,
    ) -> list[str]:
        """返回 ALTER TABLE 之后需要执行的额外 DDL 语句。

        PG 覆写：为 auto_increment 主键创建序列 + SET DEFAULT。
        """
        return []

    # ------------------------------------------------------------------
    # 索引
    # ------------------------------------------------------------------

    async def introspect_enum_types(self) -> dict[str, set[str]]:
        """Return ``{type_name: {value1, value2, ...}}`` for all enum types in the database.

        The base returns an empty dict. PostgreSQL overrides this to query pg_catalog.
        """
        return {}

    @abstractmethod
    async def introspect_indexes(self, table_name: str) -> set[str]:
        """返回表中已有索引的名称集合（不含主键）。"""
        raise NotImplementedError

    def build_sync_indexes(self, model: type[Model], existing_names: set[str]) -> list[str]:
        """返回仅缺失索引的 ``CREATE [UNIQUE] INDEX`` 语句列表。"""
        table = self.quote_identifier(model.__table__)
        result: list[str] = []
        model_columns = model.__columns__
        for idx in model.__indexes__:
            name = idx.index_name(model.__table__)
            if name in existing_names:
                continue
            cols = ", ".join(
                self.quote_identifier(model_columns[f].column_name) for f in idx.fields
            )
            unique = "UNIQUE " if idx.unique else ""
            result.append(f"CREATE {unique}INDEX {self.quote_identifier(name)} ON {table} ({cols})")
        return result

    # ------------------------------------------------------------------
    # 内部帮助方法
    # ------------------------------------------------------------------

    def _collect_writable_fields(self, instance: Model) -> tuple[list[str], list[Any]]:
        """收集可写入的字段名（原始名，未转义）和参数值。

        跳过值为 None 的 auto_increment 主键。
        dict / list 值会被序列化为 JSON 字符串。
        """
        model = type(instance)
        field_names: list[str] = []
        params: list[Any] = []
        model_column_info = model.__column_info__
        for field_name in model.__columns__:
            info = model_column_info[field_name]
            value = getattr(instance, field_name)
            if info.primary_key and info.auto_increment and value is None:
                continue
            if isinstance(value, enum.Enum):
                value = value.value
            if isinstance(value, dict | list):
                value = _json_mod.dumps(value)
            field_names.append(field_name)
            params.append(value)
        return field_names, params
