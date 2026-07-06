from __future__ import annotations

import asyncio
import datetime as _dt
import json as _json_mod
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from eorm.fields import ColumnInfo

if TYPE_CHECKING:
    from eorm.ddl import IntrospectedColumn
    from eorm.model import Model
    from eorm.query import QuerySet


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

    async def ping(self) -> bool:
        """健康检查：执行 ``SELECT 1`` 验证连接可用。"""
        try:
            row = await self.fetchrow("SELECT 1 AS ok", [])
            return row is not None and row.get("ok") == 1
        except Exception:
            return False

    # -- 断连重试 ---------------------------------------------------------

    @staticmethod
    def _is_connection_error(_exc: Exception) -> bool:
        """判断是否为连接错误（各方言覆盖）。默认不识别任何错误。"""
        return False

    async def _retry_on_connection_error(self, coro_factory: Any) -> Any:
        """连接断开时自动重试（最多 3 次，指数退避）。

        ``coro_factory`` 为无参可调用对象，每次重试时重新创建协程，
        保证从连接池获取全新连接。
        """
        max_retries = 3
        base_delay = 0.3
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return await coro_factory()
            except Exception as exc:
                last_exc = exc
                if not self._is_connection_error(exc) or attempt >= max_retries:
                    raise
                await asyncio.sleep(base_delay * (2**attempt))
        raise last_exc  # type: ignore[misc]

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
        columns = ", ".join(
            self.quote_identifier(model.__columns__[fn].column_name) for fn in field_names
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
        column_names = [
            self.quote_identifier(model.__columns__[fn].column_name) for fn in field_names
        ]

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
        for field_name, column in model.__columns__.items():
            if field_name == pk_name:
                continue

            info = model.__column_info__[field_name]
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
        for field_name, value in all_values.items():
            col = self.quote_identifier(model.__columns__[field_name].column_name)
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

        if isinstance(default, str):
            escaped = default.replace("'", "''")
            return f"DEFAULT '{escaped}'"
        if isinstance(default, bool):
            return f"DEFAULT {'TRUE' if default else 'FALSE'}"
        if isinstance(default, int | float):
            return f"DEFAULT {default}"

        return ""

    def build_create_table(self, model: type[Model]) -> str:
        """生成 ``CREATE TABLE IF NOT EXISTS ...`` 语句。"""
        table = self.quote_identifier(model.__table__)
        definitions: list[str] = []
        for field_name, column in model.__columns__.items():
            info = model.__column_info__[field_name]
            definitions.append(f"  {self._build_column_def(model, field_name, column, info)}")
        columns_sql = ",\n".join(definitions)
        return f"CREATE TABLE IF NOT EXISTS {table} (\n{columns_sql}\n)"

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
        for field_name in field_names:
            column = model.__columns__[field_name]
            info = model.__column_info__[field_name]
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

        for field_name, existing in field_pairs:
            column = model.__columns__[field_name]
            info = model.__column_info__[field_name]
            annotation = model.model_fields[field_name].annotation
            new_type = self.map_python_type(annotation, info)
            col_name = self.quote_identifier(column.column_name)

            old_normalized = self._normalize_type(existing.data_type)
            new_normalized = new_type.lower()

            type_changed = old_normalized != new_normalized
            null_changed = info.nullable != existing.is_nullable

            if not type_changed and not null_changed:
                continue

            if type_changed:
                clauses.append(f"ALTER COLUMN {col_name} TYPE {new_type}")

            if null_changed:
                if info.nullable:
                    clauses.append(f"ALTER COLUMN {col_name} DROP NOT NULL")
                else:
                    clauses.append(f"ALTER COLUMN {col_name} SET NOT NULL")

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

        # -- ADD COLUMN -------------------------------------------------
        for field_name in add_fields:
            column = model.__columns__[field_name]
            info = model.__column_info__[field_name]
            col_def = self._build_column_def(model, field_name, column, info)
            clauses.append(f"ADD COLUMN {col_def}")

        # -- MODIFY -----------------------------------------------------
        for field_name, existing in modify_pairs:
            column = model.__columns__[field_name]
            info = model.__column_info__[field_name]
            annotation = model.model_fields[field_name].annotation
            new_type = self.map_python_type(annotation, info)
            col_name = self.quote_identifier(column.column_name)

            old_normalized = self._normalize_type(existing.data_type)
            new_normalized = new_type.lower()

            type_changed = old_normalized != new_normalized
            null_changed = info.nullable != existing.is_nullable

            if type_changed:
                clauses.append(f"ALTER COLUMN {col_name} TYPE {new_type}")
            if null_changed:
                if info.nullable:
                    clauses.append(f"ALTER COLUMN {col_name} DROP NOT NULL")
                else:
                    clauses.append(f"ALTER COLUMN {col_name} SET NOT NULL")

        if not clauses:
            return ""

        sep = ",\n  "
        return f"ALTER TABLE {table}\n  {sep.join(clauses)}"

    # ------------------------------------------------------------------
    # 索引
    # ------------------------------------------------------------------

    @abstractmethod
    async def introspect_indexes(self, table_name: str) -> set[str]:
        """返回表中已有索引的名称集合（不含主键）。"""
        raise NotImplementedError

    def build_sync_indexes(
        self, model: type[Model], existing_names: set[str]
    ) -> list[str]:
        """返回仅缺失索引的 ``CREATE [UNIQUE] INDEX`` 语句列表。"""
        table = self.quote_identifier(model.__table__)
        result: list[str] = []
        for idx in model.__indexes__:
            name = idx.index_name(model.__table__)
            if name in existing_names:
                continue
            cols = ", ".join(
                self.quote_identifier(model.__columns__[f].column_name)
                for f in idx.fields
            )
            unique = "UNIQUE " if idx.unique else ""
            result.append(
                f"CREATE {unique}INDEX {self.quote_identifier(name)} "
                f"ON {table} ({cols})"
            )
        return result

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
        for field_name in model.__columns__:
            info = model.__column_info__[field_name]
            value = getattr(instance, field_name)
            if info.primary_key and info.auto_increment and value is None:
                continue
            if isinstance(value, dict | list):
                value = _json_mod.dumps(value)
            field_names.append(field_name)
            params.append(value)
        return field_names, params
