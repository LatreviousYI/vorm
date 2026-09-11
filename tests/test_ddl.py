from __future__ import annotations

import datetime
import enum
from typing import Any

import pytest

from vorm import Field, Model, Session
from vorm.ddl import Index, IntrospectedColumn
from vorm.dialects.base import AbstractDialect
from vorm.fields import ColumnInfo

# ---------------------------------------------------------------------------
# 测试方言
# ---------------------------------------------------------------------------


class DDLDialect(AbstractDialect):
    """DDL 测试用方言：type 映射贴近 PostgreSQL（双引号，INTEGER/SERIAL 等）。"""

    def render_placeholder(self, index: int) -> str:
        return f"${index}"

    def render_json_extract(self, column_sql: str, path: str, as_text: bool) -> str:
        op = "->>" if as_text else "->"
        return f"{column_sql}{op}'{path}'"

    # -- 事务 / 执行 — 不会被 DDL 测试调用，空实现即可 ---------------

    async def begin(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def execute(self, sql: str, params: list[Any]) -> Any:
        self.last_sql = sql
        self.last_params = params
        return None

    async def execute_insert(self, sql: str, params: list[Any]) -> Any:
        return None

    async def fetch(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        return []

    async def fetchrow(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        return None

    async def close(self) -> None:
        pass

    # -- DDL ----------------------------------------------------------

    def map_python_type(self, annotation: Any, column_info: ColumnInfo) -> str:
        if column_info.db_type is not None:
            return column_info.db_type

        from vorm.ddl import resolve_base_type

        base = resolve_base_type(annotation)

        # 枚举检测：Python enum.Enum → PG 自定义枚举类型
        # IntEnum 跳过（PG 不支持整型枚举），让其 fall 到 INTEGER
        if isinstance(base, type) and issubclass(base, enum.Enum):
            if not issubclass(base, int):
                return base.__name__.lower()

        if base is int:
            if column_info.auto_increment:
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
        elif base is datetime.datetime:
            return "TIMESTAMP"
        elif base is datetime.date:
            return "DATE"
        elif base in (dict, list):
            return "JSONB"
        return "TEXT"

    def build_post_create(self, model: type[Model]) -> list[str]:
        """PG 风格：生成 COMMENT ON COLUMN 语句。"""
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
        """PG 风格：处理 COMMENT ON COLUMN（新增列和注释变更）。"""
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

        # COMMENT ON COLUMN for modified columns
        for field_name, existing in modified:
            if self._comment_changed(model, field_name, existing):
                info = model.__column_info__[field_name]
                col = self.quote_identifier(model.__columns__[field_name].column_name)
                if info.comment:
                    escaped = info.comment.replace("'", "''")
                    result.append(f"COMMENT ON COLUMN {tbl}.{col} IS '{escaped}'")
                else:
                    result.append(f"COMMENT ON COLUMN {tbl}.{col} IS NULL")

        return result

    async def introspect_indexes(self, table_name: str) -> set[str]:
        return set()

    async def introspect_columns(self, table_name: str) -> dict[str, IntrospectedColumn]:
        return {}

    # -- PG 风格枚举支持 ------------------------------------------------

    async def introspect_enum_types(self) -> dict[str, set[str]]:
        """DDLDialect 默认返回空，子类可覆写。"""
        return {}

    def build_pre_create(
        self,
        model: type[Model],
        existing_enums: dict[str, set[str]] | None = None,
    ) -> list[str]:
        """PG 风格：生成 CREATE TYPE ... AS ENUM (...)。"""
        from vorm.ddl import get_enum_values, is_enum_type, resolve_base_type

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
        """PG 风格：生成 ALTER TYPE ... ADD VALUE ... 或 CREATE TYPE（新枚举）。"""
        from vorm.ddl import get_enum_values, is_enum_type, resolve_base_type

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


class RecordingDDLDialect(DDLDialect):
    """记录所有执行的 SQL，并可预设内省结果。"""

    def __init__(
        self,
        introspect_result: dict[str, IntrospectedColumn] | None = None,
        introspect_indexes: set[str] | None = None,
        introspect_enums: dict[str, set[str]] | None = None,
    ) -> None:
        self._introspect_result = introspect_result or {}
        self._introspect_indexes = introspect_indexes or set()
        self._introspect_enums = introspect_enums or {}
        self.executed_sqls: list[str] = []
        self.last_sql: str = ""
        self.last_params: list[Any] = []

    async def execute(self, sql: str, params: list[Any]) -> Any:
        self.last_sql = sql
        self.last_params = params
        self.executed_sqls.append(sql)
        return None

    async def introspect_enum_types(self) -> dict[str, set[str]]:
        return self._introspect_enums

    async def introspect_indexes(self, table_name: str) -> set[str]:
        return self._introspect_indexes

    async def introspect_columns(self, table_name: str) -> dict[str, IntrospectedColumn]:
        return self._introspect_result


# ---------------------------------------------------------------------------
# 测试模型
# ---------------------------------------------------------------------------


class User(Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto_increment=True)
    name: str = Field(max_length=100, nullable=False)
    email: str
    age: int | None = None
    is_active: bool = Field(default=True)


class Article(Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto_increment=True)
    title: str = Field(max_length=200)
    body: str | None = None
    view_count: int = Field(default=0)
    price: float | None = None


@pytest.fixture
def dialect() -> DDLDialect:
    return DDLDialect()


@pytest.fixture
def session() -> Session:
    return Session(DDLDialect())


# ---------------------------------------------------------------------------
# CREATE TABLE SQL 生成测试
# ---------------------------------------------------------------------------


def test_build_create_table_basic(dialect: DDLDialect) -> None:
    """基本的 CREATE TABLE 包含所有列和类型。"""
    sql = dialect.build_create_table(User)

    assert 'CREATE TABLE IF NOT EXISTS "users"' in sql
    assert '"id" SERIAL PRIMARY KEY' in sql
    assert '"name" VARCHAR(100) NOT NULL' in sql
    assert '"email" TEXT' in sql
    assert '"is_active" BOOLEAN' in sql


def test_build_create_table_optional_nullable(dialect: DDLDialect) -> None:
    """Optional (nullable) 列不应有 NOT NULL。"""
    sql = dialect.build_create_table(User)

    # age 是 int | None，默认 nullable=True → 无 NOT NULL
    assert '"age" INTEGER' in sql
    assert '"age" INTEGER NOT NULL' not in sql


def test_build_create_table_unique_constraint(dialect: DDLDialect) -> None:
    """unique=True 的列应包含 UNIQUE 约束。"""
    sql = dialect.build_create_table(Article)
    assert '"id" SERIAL PRIMARY KEY' in sql
    assert '"title" VARCHAR(200)' in sql


def test_build_create_table_non_nullable(dialect: DDLDialect) -> None:
    """nullable=False 的列应有 NOT NULL。"""
    sql = dialect.build_create_table(User)
    assert '"name" VARCHAR(100) NOT NULL' in sql


def test_build_create_table_db_type_override(dialect: DDLDialect) -> None:
    """db_type 覆盖应跳过自动推断。"""

    class Custom(Model):
        class Meta:
            table = "customs"

        id: int = Field(primary_key=True, auto_increment=True)
        data: str = Field(db_type="JSON")

    sql = dialect.build_create_table(Custom)
    assert '"data" JSON' in sql


def test_build_create_table_json_dict(dialect: DDLDialect) -> None:
    """dict 类型应自动映射为 JSONB 类型。"""

    class Product(Model):
        class Meta:
            table = "products"

        id: int = Field(primary_key=True, auto_increment=True)
        data: dict

    sql = dialect.build_create_table(Product)
    assert '"data" JSONB' in sql


def test_build_create_table_json_list(dialect: DDLDialect) -> None:
    """list 类型应自动映射为 JSONB 类型。"""

    class Product(Model):
        class Meta:
            table = "products"

        id: int = Field(primary_key=True, auto_increment=True)
        tags: list

    sql = dialect.build_create_table(Product)
    assert '"tags" JSONB' in sql


def test_build_create_table_json_default_dict(dialect: DDLDialect) -> None:
    """dict 字段有 default_factory=dict 时应生成 jsonb_build_object() 原生函数。"""

    class Product(Model):
        class Meta:
            table = "products"

        id: int = Field(primary_key=True, auto_increment=True)
        config: dict = Field(default_factory=dict)

    sql = dialect.build_create_table(Product)
    assert '"config" JSONB' in sql
    assert "DEFAULT" in sql
    assert "jsonb_build_object()" in sql


def test_build_create_table_json_default_list(dialect: DDLDialect) -> None:
    """list 字段有 default_factory=list 时应生成 jsonb_build_array() 原生函数。"""

    class Product(Model):
        class Meta:
            table = "products"

        id: int = Field(primary_key=True, auto_increment=True)
        tags: list = Field(default_factory=list)

    sql = dialect.build_create_table(Product)
    assert '"tags" JSONB' in sql
    assert "DEFAULT" in sql
    assert "jsonb_build_array()" in sql


def test_build_create_table_json_default_dict_with_values(dialect: DDLDialect) -> None:
    """dict 字段有初始值时应生成 jsonb_build_object('key', 'value')。"""

    class Product(Model):
        class Meta:
            table = "products"

        id: int = Field(primary_key=True, auto_increment=True)
        config: dict = Field(default_factory=lambda: {"theme": "dark", "count": 2})

    sql = dialect.build_create_table(Product)
    assert '"config" JSONB' in sql
    assert "DEFAULT" in sql
    assert "jsonb_build_object" in sql
    assert "'theme'" in sql
    assert "'dark'" in sql
    assert "'count'" in sql
    assert "2" in sql
    # 不应包含 json.dumps 风格的字符串（如 ''dark'' 等双引号）
    assert "'dark'" in sql  # SQL 字符串字面量，不是 JSON 序列化


def test_build_create_table_json_default_nested(dialect: DDLDialect) -> None:
    """嵌套 dict/list 默认值应递归生成原生函数调用。"""

    class Product(Model):
        class Meta:
            table = "products"

        id: int = Field(primary_key=True, auto_increment=True)
        meta: dict = Field(default_factory=lambda: {"tags": ["a", "b"], "count": 1})

    sql = dialect.build_create_table(Product)
    assert '"meta" JSONB' in sql
    assert "DEFAULT" in sql
    assert "jsonb_build_object" in sql
    assert "jsonb_build_array" in sql
    assert "'a'" in sql
    assert "'b'" in sql


def test_build_create_table_custom_column_name(dialect: DDLDialect) -> None:
    """column_name 参数使用自定义列名。"""

    class Renamed(Model):
        class Meta:
            table = "renamed"

        id: int = Field(primary_key=True, auto_increment=True)
        py_name: str = Field(column_name="db_name")

    sql = dialect.build_create_table(Renamed)
    assert '"db_name" TEXT' in sql
    assert '"py_name"' not in sql


def test_build_create_table_multiple_columns(dialect: DDLDialect) -> None:
    """多列模型每列都应在 CREATE TABLE 中出现。"""
    sql = dialect.build_create_table(Article)

    assert '"id" SERIAL PRIMARY KEY' in sql
    assert '"title" VARCHAR(200)' in sql
    assert '"body" TEXT' in sql
    assert '"view_count" INTEGER' in sql
    assert '"price" DOUBLE PRECISION' in sql


# ---------------------------------------------------------------------------
# ALTER TABLE ADD COLUMN SQL 生成测试
# ---------------------------------------------------------------------------


def test_build_add_column_basic(dialect: DDLDialect) -> None:
    """ALTER TABLE ADD COLUMN 应包含正确的类型和约束。"""
    sql = dialect.build_add_column(Article, "price")

    assert 'ALTER TABLE "articles" ADD COLUMN' in sql
    assert '"price" DOUBLE PRECISION' in sql


def test_build_add_column_not_null(dialect: DDLDialect) -> None:
    """ADD COLUMN 对 non-nullable 字段应包含 NOT NULL。"""
    # name 是 str, nullable=False
    sql = dialect.build_add_column(User, "name")

    assert 'ALTER TABLE "users" ADD COLUMN' in sql
    assert '"name" VARCHAR(100) NOT NULL' in sql


def test_build_add_column_custom_name(dialect: DDLDialect) -> None:
    """ADD COLUMN 使用 column_name。"""

    class Renamed(Model):
        class Meta:
            table = "renamed"

        id: int = Field(primary_key=True, auto_increment=True)
        py_name: str = Field(column_name="db_name")

    sql = dialect.build_add_column(Renamed, "py_name")
    assert '"db_name" TEXT' in sql


def test_build_add_columns_multiple(dialect: DDLDialect) -> None:
    """build_add_columns 将多列合并为一条 ALTER TABLE。"""
    sql = dialect.build_add_columns(User, ["email", "age", "is_active"])

    assert sql.count("ALTER TABLE") == 1
    assert 'ADD COLUMN "email"' in sql
    assert 'ADD COLUMN "age"' in sql
    assert 'ADD COLUMN "is_active"' in sql
    # 三列之间应有逗号分隔
    assert sql.count("ADD COLUMN") == 3
    assert sql.count(",\n") == 2  # 两个逗号（第一列和第二列之间，第二列和第三列之间）


# ---------------------------------------------------------------------------
# sync_table 执行测试
# ---------------------------------------------------------------------------


async def test_sync_table_creates_when_table_missing() -> None:
    """表不存在时 sync_table 应执行 CREATE TABLE。"""
    dialect = RecordingDDLDialect(introspect_result={})
    session = Session(dialect)

    await User.sync_table(session.dialect)

    assert len(dialect.executed_sqls) == 1
    assert "CREATE TABLE IF NOT EXISTS" in dialect.executed_sqls[0]


async def test_sync_table_adds_new_columns() -> None:
    """表存在但缺少某些列时，sync_table 将所有缺失列合并为一条 ALTER TABLE。"""
    existing = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await User.sync_table(session.dialect)

    # 只执行一条 ALTER TABLE（PK 被跳过，只有 ADD COLUMN）
    assert len(dialect.executed_sqls) == 1
    sql = dialect.executed_sqls[0]
    assert "ALTER TABLE" in sql
    # 所有缺失列（name, email, age, is_active）都应出现
    assert '"name"' in sql
    assert '"email"' in sql
    assert '"age"' in sql
    assert '"is_active"' in sql
    # 多列合并在一条语句中
    assert sql.count("ADD COLUMN") == 4


async def test_sync_table_noop_when_all_columns_exist() -> None:
    """所有列都已存在且类型匹配时，sync_table 不应执行任何 SQL。"""
    existing = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
        "name": IntrospectedColumn("name", "varchar(100)", is_nullable=False),
        "email": IntrospectedColumn("email", "text", is_nullable=True),
        "age": IntrospectedColumn("age", "integer", is_nullable=True),
        "is_active": IntrospectedColumn(
            "is_active", "boolean", is_nullable=True, column_default="true"
        ),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await User.sync_table(session.dialect)

    assert len(dialect.executed_sqls) == 0


class _CaptureAlterDialect(RecordingDDLDialect):
    """记录传给 build_sync_alter 的修改列，用于验证 sync_table 的列变更判定。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.modify_pairs: list[tuple[str, IntrospectedColumn]] = []

    def build_sync_alter(
        self,
        model: type[Model],
        add_fields: list[str],
        modify_pairs: list[tuple[str, IntrospectedColumn]],
    ) -> str:
        self.modify_pairs = list(modify_pairs)
        return super().build_sync_alter(model, add_fields, modify_pairs)


async def test_sync_table_reports_no_modified_columns_when_all_match() -> None:
    """重复同步时所有列都匹配，不应有任何列被误判为"修改"。"""
    existing = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
        "name": IntrospectedColumn("name", "varchar(100)", is_nullable=False),
        "email": IntrospectedColumn("email", "text", is_nullable=True),
        "age": IntrospectedColumn("age", "integer", is_nullable=True),
        "is_active": IntrospectedColumn(
            "is_active", "boolean", is_nullable=True, column_default="true"
        ),
    }
    dialect = _CaptureAlterDialect(introspect_result=existing)
    session = Session(dialect)

    await User.sync_table(session.dialect)

    assert dialect.modify_pairs == []
    assert len(dialect.executed_sqls) == 0


async def test_sync_table_case_insensitive_match() -> None:
    """列名匹配应大小写不敏感。"""
    # 真实 DB 返回大写列名 "ID"，但 introspect_columns 将其 key 小写化
    existing = {
        "id": IntrospectedColumn("ID", "serial", is_nullable=False),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await User.sync_table(session.dialect)

    # id 已存在（大小写不同），不应为其生成 ADD COLUMN
    add_sqls = [s for s in dialect.executed_sqls if "ADD COLUMN" in s]
    assert not any('"id"' in s for s in add_sqls)


# ---------------------------------------------------------------------------
# 列修改 SQL 生成测试
# ---------------------------------------------------------------------------


def test_build_modify_columns_type_change(dialect: DDLDialect) -> None:
    """类型变更（如扩大 VARCHAR）应生成 ALTER COLUMN TYPE。"""
    existing = IntrospectedColumn("name", "varchar", is_nullable=False)

    sql = dialect.build_modify_columns(User, [("name", existing)])

    assert "ALTER TABLE" in sql
    assert '"name"' in sql
    # DDLDialect 是 PG 风格：ALTER COLUMN ... TYPE ...
    assert "ALTER COLUMN" in sql
    assert "TYPE" in sql
    assert "VARCHAR(100)" in sql


def test_build_modify_columns_nullability_change(dialect: DDLDialect) -> None:
    """null 约束变更应生成 SET/DROP NOT NULL。"""
    # body 字段（str | None，nullable=True）→ DB 里是 NOT NULL → 应 DROP NOT NULL
    existing_body = IntrospectedColumn("body", "varchar", is_nullable=False)

    sql = dialect.build_modify_columns(Article, [("body", existing_body)])

    assert "ALTER TABLE" in sql
    assert '"body"' in sql
    assert "DROP NOT NULL" in sql


def test_build_modify_columns_both_changes(dialect: DDLDialect) -> None:
    """同一列既有类型变更又有 null 变更。"""
    existing = IntrospectedColumn("name", "varchar", is_nullable=True)

    sql = dialect.build_modify_columns(User, [("name", existing)])

    assert "ALTER TABLE" in sql
    assert '"name"' in sql
    assert "TYPE" in sql
    assert "NOT NULL" in sql


def test_build_modify_columns_multiple_columns(dialect: DDLDialect) -> None:
    """多个列需要修改时合并为一条 ALTER。"""
    existing_name = IntrospectedColumn("name", "varchar", is_nullable=True)
    existing_email = IntrospectedColumn("email", "varchar", is_nullable=True)

    sql = dialect.build_modify_columns(User, [("name", existing_name), ("email", existing_email)])

    assert sql.count("ALTER TABLE") == 1
    assert '"name"' in sql
    assert '"email"' in sql


def test_build_modify_columns_no_diff_skips(dialect: DDLDialect) -> None:
    """类型和 null 都匹配时应返回空（无 ALTER）。"""
    # DDLDialect.map_python_type 对 str + max_length=100 → VARCHAR(100)
    existing = IntrospectedColumn("name", "varchar(100)", is_nullable=False)

    sql = dialect.build_modify_columns(User, [("name", existing)])

    # 类型匹配（varchar(100) == VARCHAR(100) 大小写不敏感）、null 匹配 → 无 SQL
    assert sql == ""


def test_column_changed_no_diff(dialect: DDLDialect) -> None:
    """类型 / null / 默认值 / 注释都匹配 → 判定为无需修改。"""
    existing = IntrospectedColumn("name", "varchar(100)", is_nullable=False)
    assert dialect.column_changed(User, "name", existing) is False


def test_column_changed_type_diff(dialect: DDLDialect) -> None:
    """类型不一致 → 判定为需要修改。"""
    existing = IntrospectedColumn("name", "varchar(50)", is_nullable=False)
    assert dialect.column_changed(User, "name", existing) is True


def test_column_changed_null_diff(dialect: DDLDialect) -> None:
    """null 约束不一致 → 判定为需要修改。"""
    existing = IntrospectedColumn("name", "varchar(100)", is_nullable=True)
    assert dialect.column_changed(User, "name", existing) is True


def test_column_changed_comment_diff(dialect: DDLDialect) -> None:
    """注释不一致 → 判定为需要修改（PG 由 build_post_alter 处理 COMMENT）。"""
    existing = IntrospectedColumn(
        "name", "varchar(100)", is_nullable=True, column_comment="Old comment"
    )
    assert dialect.column_changed(CommentModel, "name", existing) is True


# ---------------------------------------------------------------------------
# sync_table 修改列执行测试
# ---------------------------------------------------------------------------


async def test_sync_table_modifies_varchar_length() -> None:
    """VARCHAR 长度变化时 sync_table 应执行 ALTER COLUMN TYPE。"""
    existing = {
        "id": IntrospectedColumn("id", "integer", is_nullable=False),
        "name": IntrospectedColumn("name", "varchar(50)", is_nullable=False),
        "email": IntrospectedColumn("email", "varchar", is_nullable=True),
        "age": IntrospectedColumn("age", "integer", is_nullable=True),
        "is_active": IntrospectedColumn("is_active", "boolean", is_nullable=False),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await User.sync_table(session.dialect)

    # name 从 varchar(50) → varchar(100) → 应有一条 MODIFY
    modify_sqls = [s for s in dialect.executed_sqls if "ALTER COLUMN" in s or "MODIFY" in s]
    assert len(modify_sqls) >= 1


async def test_sync_table_modifies_nullability() -> None:
    """null 约束变化时 sync_table 应执行 ALTER COLUMN DROP NOT NULL。"""
    existing = {
        "id": IntrospectedColumn("id", "integer", is_nullable=False),
        "name": IntrospectedColumn("name", "varchar(100)", is_nullable=True),
        # 模型里 name 是 nullable=False，DB 里是 nullable=True
        "email": IntrospectedColumn("email", "varchar", is_nullable=True),
        "age": IntrospectedColumn("age", "integer", is_nullable=True),
        "is_active": IntrospectedColumn("is_active", "boolean", is_nullable=False),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await User.sync_table(session.dialect)

    # name 的 null 约束变了 → 应有 ALTER
    modify_sqls = [
        s
        for s in dialect.executed_sqls
        if "ALTER COLUMN" in s or "SET NOT NULL" in s or "DROP NOT NULL" in s
    ]
    assert len(modify_sqls) >= 1
    assert any("SET NOT NULL" in s for s in modify_sqls)


async def test_sync_table_new_columns_and_modifications_combined() -> None:
    """同时有新列和修改列时，各自生成对应的 ALTER。"""
    existing = {
        "id": IntrospectedColumn("id", "integer", is_nullable=False),
        "name": IntrospectedColumn("name", "varchar(50)", is_nullable=True),
        # email, age, is_active 缺失
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await User.sync_table(session.dialect)

    # ADD 和 MODIFY 应合并为一条 ALTER TABLE
    assert len(dialect.executed_sqls) == 1
    sql = dialect.executed_sqls[0]
    assert "ADD COLUMN" in sql
    assert "ALTER COLUMN" in sql or "MODIFY" in sql


# ---------------------------------------------------------------------------
# 默认值变更检测
# ---------------------------------------------------------------------------


class DefaultTestModel(Model):
    """带默认值的模型，用于测试 sync_table 的默认值变更检测。"""

    class Meta:
        table = "default_test"

    id: int = Field(primary_key=True, auto_increment=True)
    name: str = Field(default="hello")
    status: str = Field(default="active", max_length=20)


async def test_sync_table_default_unchanged() -> None:
    """当默认值未变化时，不应触发 MODIFY。"""
    existing = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
        "name": IntrospectedColumn(
            "name",
            "text",
            is_nullable=True,
            column_default="'hello'::character varying",
        ),
        "status": IntrospectedColumn(
            "status",
            "varchar(20)",
            is_nullable=True,
            column_default="'active'::character varying",
        ),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await DefaultTestModel.sync_table(session.dialect)

    assert len(dialect.executed_sqls) == 0


async def test_sync_table_default_added() -> None:
    """模型中新增了默认值但 DB 中无默认值时，应触发 SET DEFAULT。"""
    existing = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
        "name": IntrospectedColumn("name", "varchar", is_nullable=True, column_default=None),
        "status": IntrospectedColumn(
            "status", "varchar(20)", is_nullable=True, column_default=None
        ),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await DefaultTestModel.sync_table(session.dialect)

    sql = " ".join(dialect.executed_sqls)
    assert "SET DEFAULT" in sql
    assert "'hello'" in sql
    assert "'active'" in sql


async def test_sync_table_default_removed() -> None:
    """模型中移除了默认值但 DB 中还残留时，应触发 DROP DEFAULT。"""
    # 用无默认值的 User 模型，但 DB 有残留默认值
    existing = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
        "name": IntrospectedColumn(
            "name", "varchar(100)", is_nullable=False, column_default="'old'"
        ),
        "email": IntrospectedColumn("email", "text", is_nullable=True),
        "age": IntrospectedColumn("age", "integer", is_nullable=True),
        "is_active": IntrospectedColumn(
            "is_active", "boolean", is_nullable=True, column_default="true"
        ),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await User.sync_table(session.dialect)

    sql = " ".join(dialect.executed_sqls)
    # name: 模型中无默认值，DB 有 'old' → DROP DEFAULT
    # is_active: 模型有 default=True，DB 也有 true → 一致，不会触发
    assert "DROP DEFAULT" in sql
    assert "name" in sql


async def test_sync_table_default_value_changed() -> None:
    """模型中默认值变更时，应触发 SET DEFAULT。"""
    existing = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
        "name": IntrospectedColumn(
            "name",
            "varchar",
            is_nullable=True,
            column_default="'world'::character varying",
        ),
        "status": IntrospectedColumn(
            "status",
            "varchar(20)",
            is_nullable=True,
            column_default="'active'::character varying",
        ),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await DefaultTestModel.sync_table(session.dialect)

    sql = " ".join(dialect.executed_sqls)
    # name 默认值变了：world → hello
    assert "SET DEFAULT 'hello'" in sql or "SET DEFAULT" in sql
    # status 默认值未变：active → active，不应出现
    assert "status" not in sql


# ---------------------------------------------------------------------------
# 索引测试
# ---------------------------------------------------------------------------


class IndexedModel(Model):
    """带联合索引的模型。"""

    class Meta:
        table = "indexed"
        indexes = [
            Index(fields=("name", "age")),  # 联合普通索引
            Index(fields=("email",), unique=True),  # 唯一索引
            Index(fields=("name", "age", "status"), name="idx_custom"),
        ]

    id: int = Field(primary_key=True, auto_increment=True)
    name: str = Field(max_length=100, nullable=False)
    age: int
    email: str = Field(max_length=255)
    status: str = Field(max_length=20)


def test_index_name_auto_generation() -> None:
    """未指定 name 时自动生成 ix_{table}_{fields} 或 unq_{table}_{fields}。"""
    ix = Index(fields=("name", "age"))
    assert ix.index_name("indexed") == "ix_indexed_name_age"

    unq = Index(fields=("email",), unique=True)
    assert unq.index_name("indexed") == "unq_indexed_email"

    named = Index(fields=("a", "b"), name="my_idx")
    assert named.index_name("indexed") == "my_idx"


def test_build_sync_indexes_generates_all_when_none_exist(dialect: DDLDialect) -> None:
    """数据库中无任何索引时，应生成所有索引的 CREATE 语句。"""
    sqls = dialect.build_sync_indexes(IndexedModel, existing_names=set())
    assert len(sqls) == 3

    # 普通联合索引
    assert any("ix_indexed_name_age" in s for s in sqls)
    assert any("CREATE INDEX" in s and "UNIQUE" not in s for s in sqls)
    # 唯一索引
    assert any("unq_indexed_email" in s for s in sqls)
    assert any("CREATE UNIQUE INDEX" in s for s in sqls)
    # 自定义名
    assert any("idx_custom" in s for s in sqls)


def test_build_sync_indexes_skips_existing(dialect: DDLDialect) -> None:
    """已有索引应被跳过，不重复生成。"""
    existing = {"ix_indexed_name_age", "unq_indexed_email"}
    sqls = dialect.build_sync_indexes(IndexedModel, existing_names=existing)
    assert len(sqls) == 1
    assert "idx_custom" in sqls[0]


async def test_sync_table_creates_indexes() -> None:
    """sync_table 应在建表后创建缺失的索引。"""
    existing_cols = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
        "name": IntrospectedColumn("name", "varchar(100)", is_nullable=False),
        "age": IntrospectedColumn("age", "integer", is_nullable=True),
        "email": IntrospectedColumn("email", "varchar(255)", is_nullable=True),
        "status": IntrospectedColumn("status", "varchar(20)", is_nullable=True),
    }
    dialect = RecordingDDLDialect(
        introspect_result=existing_cols,
        introspect_indexes=set(),  # 无已有索引
    )
    session = Session(dialect)

    await IndexedModel.sync_table(session.dialect)

    # ALTER TABLE（列已全匹配，无变更）+ 3 条 CREATE INDEX
    assert len(dialect.executed_sqls) == 3
    # 全部是 CREATE INDEX
    assert all("CREATE" in s and "INDEX" in s for s in dialect.executed_sqls)


async def test_sync_table_skips_existing_indexes() -> None:
    """已有索引不应被重复创建。"""
    dialect = RecordingDDLDialect(
        introspect_result={
            "id": IntrospectedColumn("id", "serial", is_nullable=False),
            "name": IntrospectedColumn("name", "varchar(100)", is_nullable=False),
            "age": IntrospectedColumn("age", "integer", is_nullable=True),
            "email": IntrospectedColumn("email", "varchar(255)", is_nullable=True),
            "status": IntrospectedColumn("status", "varchar(20)", is_nullable=True),
        },
        introspect_indexes={  # 3 个已存在 1 个缺失
            "ix_indexed_name_age",
            "unq_indexed_email",
            "idx_custom",
        },
    )
    session = Session(dialect)

    await IndexedModel.sync_table(session.dialect)
    # 无新增列、无类型变化、无缺失索引 → 0 条 SQL
    assert len(dialect.executed_sqls) == 0


class FieldIndexModel(Model):
    """使用 Field(index=True) 声明单列索引。"""

    class Meta:
        table = "field_index"

    id: int = Field(primary_key=True, auto_increment=True)
    name: str = Field(max_length=50)
    email: str = Field(index=True, max_length=255)


def test_field_index_becomes_auto_index() -> None:
    """Field(index=True) 应自动生成 Index 对象。"""
    indexes = FieldIndexModel.__indexes__
    assert len(indexes) == 1
    assert indexes[0].fields == ("email",)
    assert indexes[0].unique is False
    assert indexes[0].index_name("field_index") == "ix_field_index_email"


def test_build_sync_indexes_for_field_index(dialect: DDLDialect) -> None:
    """Field(index=True) 的索引应出现在 CREATE INDEX 中。"""
    sqls = dialect.build_sync_indexes(FieldIndexModel, existing_names=set())
    assert len(sqls) == 1
    assert "ix_field_index_email" in sqls[0]


# ---------------------------------------------------------------------------
# 列注释测试模型
# ---------------------------------------------------------------------------


class CommentModel(Model):
    """带列注释的模型，用于测试 COMMENT DDL 生成。"""

    class Meta:
        table = "comment_test"

    id: int = Field(primary_key=True, auto_increment=True)
    name: str = Field(max_length=100, comment="User display name")
    email: str = Field(comment="Email address")
    bio: str | None = None  # 无注释


# ---------------------------------------------------------------------------
# PG 风格 COMMENT ON COLUMN SQL 生成测试
# ---------------------------------------------------------------------------


def test_build_post_create_comment(dialect: DDLDialect) -> None:
    """build_post_create 应为有 comment 的列生成 COMMENT ON COLUMN 语句。"""
    sqls = dialect.build_post_create(CommentModel)

    assert len(sqls) == 2
    assert 'COMMENT ON COLUMN "comment_test"."name" IS' in sqls[0]
    assert "User display name" in sqls[0]
    assert 'COMMENT ON COLUMN "comment_test"."email" IS' in sqls[1]
    assert "Email address" in sqls[1]


def test_build_post_create_no_comment_model(dialect: DDLDialect) -> None:
    """没有注释的模型 build_post_create 应返回空列表。"""
    sqls = dialect.build_post_create(User)
    assert sqls == []


def test_build_create_table_without_comment(dialect: DDLDialect) -> None:
    """PG 的 CREATE TABLE 列定义中不应包含 COMMENT（PG 不支持内联注释）。"""
    sql = dialect.build_create_table(CommentModel)
    assert "COMMENT" not in sql


def test_build_post_alter_comment_new_column(dialect: DDLDialect) -> None:
    """新增带注释的列时，build_post_alter 应生成 COMMENT ON COLUMN。"""
    sqls = dialect.build_post_alter(CommentModel, modified=[], add_fields=["name", "email"])

    assert any("COMMENT ON COLUMN" in s for s in sqls)
    assert any("User display name" in s for s in sqls)
    assert any("Email address" in s for s in sqls)


def test_build_post_alter_comment_changed(dialect: DDLDialect) -> None:
    """注释变化时应生成 COMMENT ON COLUMN。"""
    existing_name = IntrospectedColumn(
        "name", "varchar(100)", is_nullable=True, column_comment="Old comment"
    )
    sqls = dialect.build_post_alter(CommentModel, modified=[("name", existing_name)])

    assert any("COMMENT ON COLUMN" in s for s in sqls)
    assert any("User display name" in s for s in sqls)


def test_build_post_alter_comment_unchanged(dialect: DDLDialect) -> None:
    """注释未变化时不生成任何 DDL。"""
    existing_name = IntrospectedColumn(
        "name", "varchar(100)", is_nullable=True, column_comment="User display name"
    )
    sqls = dialect.build_post_alter(CommentModel, modified=[("name", existing_name)])

    assert sqls == []


def test_build_post_alter_comment_removed(dialect: DDLDialect) -> None:
    """模型中无注释但 DB 中有 → 生成 IS NULL 删除注释。"""
    existing_bio = IntrospectedColumn("bio", "text", is_nullable=True, column_comment="Old bio")
    sqls = dialect.build_post_alter(CommentModel, modified=[("bio", existing_bio)])

    assert any("COMMENT ON COLUMN" in s and "IS NULL" in s for s in sqls)
    assert any("bio" in s for s in sqls)


# ---------------------------------------------------------------------------
# sync_table 端到端注释测试（PG 风格）
# ---------------------------------------------------------------------------


async def test_sync_table_creates_with_comments() -> None:
    """新建表时：CREATE TABLE + COMMENT ON COLUMN（通过 build_post_create）。"""
    dialect = RecordingDDLDialect(introspect_result={})
    session = Session(dialect)

    await CommentModel.sync_table(session.dialect)

    # CREATE TABLE + 2 COMMENT ON COLUMN
    assert len(dialect.executed_sqls) == 3
    assert "CREATE TABLE IF NOT EXISTS" in dialect.executed_sqls[0]
    assert "COMMENT ON COLUMN" in dialect.executed_sqls[1]
    assert "COMMENT ON COLUMN" in dialect.executed_sqls[2]


async def test_sync_table_add_column_with_comment() -> None:
    """新增带注释列时：ALTER TABLE ADD COLUMN + COMMENT ON COLUMN。"""
    existing = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await CommentModel.sync_table(session.dialect)

    alter_sqls = [s for s in dialect.executed_sqls if "ALTER TABLE" in s]
    comment_sqls = [s for s in dialect.executed_sqls if "COMMENT ON COLUMN" in s]
    assert len(alter_sqls) == 1
    assert len(comment_sqls) == 2  # name + email
    assert any("User display name" in s for s in comment_sqls)


async def test_sync_table_comment_changed() -> None:
    """注释变更时 sync_table 应生成 COMMENT ON COLUMN。"""
    existing = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
        "name": IntrospectedColumn(
            "name", "varchar(100)", is_nullable=True, column_comment="Old name"
        ),
        "email": IntrospectedColumn("email", "text", is_nullable=True, column_default=None),
        "bio": IntrospectedColumn("bio", "text", is_nullable=True),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await CommentModel.sync_table(session.dialect)

    comment_sqls = [s for s in dialect.executed_sqls if "COMMENT ON COLUMN" in s]
    # name: old → new, email: None → "Email address"
    assert len(comment_sqls) == 2
    assert any("User display name" in s for s in comment_sqls)
    assert any("Email address" in s for s in comment_sqls)


async def test_sync_table_noop_when_comments_match() -> None:
    """列和注释都匹配时 sync_table 不应执行任何 SQL。"""
    existing = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
        "name": IntrospectedColumn(
            "name", "varchar(100)", is_nullable=True, column_comment="User display name"
        ),
        "email": IntrospectedColumn(
            "email", "text", is_nullable=True, column_comment="Email address"
        ),
        "bio": IntrospectedColumn("bio", "text", is_nullable=True),
    }
    dialect = RecordingDDLDialect(introspect_result=existing)
    session = Session(dialect)

    await CommentModel.sync_table(session.dialect)

    assert len(dialect.executed_sqls) == 0


# ---------------------------------------------------------------------------
# MySQL 风格内联 COMMENT 测试
# ---------------------------------------------------------------------------


class MySQLTestDialect(AbstractDialect):
    """最小 MySQL 风格测试方言，验证内联 COMMENT 语法。"""

    quote_char = "`"

    def render_placeholder(self, index: int) -> str:
        return "%s"

    def render_json_extract(self, column_sql: str, path: str, as_text: bool) -> str:
        return f"{column_sql}->'{path}'"

    def map_python_type(self, annotation: Any, column_info: ColumnInfo) -> str:
        from vorm.ddl import get_enum_values, resolve_base_type

        base = resolve_base_type(annotation)

        # 枚举检测：Python enum → MySQL ENUM('val1','val2',...)
        # IntEnum 不生成 ENUM（MySQL ENUM 只支持字符串值）
        if isinstance(base, type) and issubclass(base, enum.Enum):
            if column_info.db_type:
                return column_info.db_type
            if issubclass(base, int):
                return "INT"
            values = get_enum_values(annotation)
            quoted = ",".join(f"'{v}'" for v in values)
            return f"ENUM({quoted})"

        if base is int:
            return "INT"
        elif base is str:
            if column_info.max_length is not None:
                return f"VARCHAR({column_info.max_length})"
            return "TEXT"
        return "TEXT"

    def build_modify_columns(
        self,
        model: type[Model],
        field_pairs: list[tuple[str, IntrospectedColumn]],
    ) -> str:
        """MySQL 风格：MODIFY COLUMN 语法。"""
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
            comment_changed = self._comment_changed(model, field_name, existing)

            if (
                not type_changed
                and not null_changed
                and not default_changed
                and not comment_changed
            ):
                continue

            col_name = self.quote_identifier(column.column_name)
            parts = [col_name, new_type]
            if not info.nullable:
                parts.append("NOT NULL")
            else:
                parts.append("NULL")
            if not info.auto_increment:
                default_clause = self._render_default_clause(
                    model.model_fields[field_name], new_type
                )
                if default_clause:
                    parts.append(default_clause)
            if info.comment:
                escaped = info.comment.replace("'", "''")
                parts.append(f"COMMENT '{escaped}'")
            col_defs.append(f"MODIFY COLUMN {' '.join(parts)}")

        if not col_defs:
            return ""

        sep = ",\n  "
        return f"ALTER TABLE {table}\n  {sep.join(col_defs)}"

    def _build_column_def(
        self,
        model: type[Model],
        field_name: str,
        column: Any,
        info: ColumnInfo,
    ) -> str:
        """MySQL 风格：内联 COMMENT。"""
        base_def = super()._build_column_def(model, field_name, column, info)
        if info.comment:
            escaped = info.comment.replace("'", "''")
            base_def += f" COMMENT '{escaped}'"
        return base_def

    # -- 以下纯虚方法用 no-op 填充 --

    async def begin(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    async def execute(self, sql: str, params: list[Any]) -> Any:
        return None

    async def execute_insert(self, sql: str, params: list[Any]) -> Any:
        return None

    async def fetch(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        return []

    async def fetchrow(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        return None

    async def close(self) -> None:
        pass

    async def introspect_columns(self, table_name: str) -> dict[str, IntrospectedColumn]:
        return {}

    async def introspect_indexes(self, table_name: str) -> set[str]:
        return set()


@pytest.fixture
def mysql_dialect() -> MySQLTestDialect:
    return MySQLTestDialect()


def test_mysql_build_column_def_with_comment(mysql_dialect: MySQLTestDialect) -> None:
    """MySQL _build_column_def 应在列定义末尾追加 COMMENT。"""
    col_def = mysql_dialect._build_column_def(
        CommentModel,
        "name",
        CommentModel.__columns__["name"],
        CommentModel.__column_info__["name"],
    )

    assert "`name`" in col_def
    assert "VARCHAR(100)" in col_def
    assert "COMMENT 'User display name'" in col_def


def test_mysql_build_column_def_without_comment(mysql_dialect: MySQLTestDialect) -> None:
    """无注释时 _build_column_def 不应包含 COMMENT。"""
    col_def = mysql_dialect._build_column_def(
        CommentModel,
        "bio",
        CommentModel.__columns__["bio"],
        CommentModel.__column_info__["bio"],
    )

    assert "COMMENT" not in col_def


def test_mysql_build_create_table_with_comment(mysql_dialect: MySQLTestDialect) -> None:
    """MySQL CREATE TABLE 中应内联 COMMENT。"""
    sql = mysql_dialect.build_create_table(CommentModel)

    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "COMMENT 'User display name'" in sql
    assert "COMMENT 'Email address'" in sql


def test_mysql_comment_escapes_single_quote(mysql_dialect: MySQLTestDialect) -> None:
    """注释中的单引号应被转义。"""

    class QuoteModel(Model):
        class Meta:
            table = "quote_test"

        id: int = Field(primary_key=True, auto_increment=True)
        name: str = Field(comment="User's data")

    col_def = mysql_dialect._build_column_def(
        QuoteModel,
        "name",
        QuoteModel.__columns__["name"],
        QuoteModel.__column_info__["name"],
    )

    assert "COMMENT 'User''s data'" in col_def


# ---------------------------------------------------------------------------
# 枚举辅助函数测试
# ---------------------------------------------------------------------------


class _TestStrEnum(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class _TestIntEnum(int, enum.Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


class _TestPlainEnum(enum.Enum):
    FOO = "foo"
    BAR = "bar"


def test_is_enum_type_with_str_enum():
    from vorm.ddl import is_enum_type

    assert is_enum_type(_TestStrEnum) is True


def test_is_enum_type_with_int_enum():
    from vorm.ddl import is_enum_type

    assert is_enum_type(_TestIntEnum) is True


def test_is_enum_type_with_plain_enum():
    from vorm.ddl import is_enum_type

    assert is_enum_type(_TestPlainEnum) is True


def test_is_enum_type_with_optional_enum():
    from vorm.ddl import is_enum_type

    assert is_enum_type(_TestStrEnum | None) is True
    assert is_enum_type(None | _TestStrEnum) is True


def test_is_enum_type_with_plain_str():
    from vorm.ddl import is_enum_type

    assert is_enum_type(str) is False


def test_is_enum_type_with_int():
    from vorm.ddl import is_enum_type

    assert is_enum_type(int) is False


def test_get_enum_values_str_enum():
    from vorm.ddl import get_enum_values

    assert get_enum_values(_TestStrEnum) == ["draft", "published", "archived"]


def test_get_enum_values_int_enum():
    from vorm.ddl import get_enum_values

    assert get_enum_values(_TestIntEnum) == [1, 2, 3]


def test_get_enum_values_plain_enum():
    from vorm.ddl import get_enum_values

    assert get_enum_values(_TestPlainEnum) == ["foo", "bar"]


def test_get_enum_values_with_optional():
    from vorm.ddl import get_enum_values

    assert get_enum_values(_TestStrEnum | None) == ["draft", "published", "archived"]


# ---------------------------------------------------------------------------
# _render_default_clause 枚举默认值测试
# ---------------------------------------------------------------------------


class _DefaultStrEnumModel(Model):
    class Meta:
        table = "default_str_enum"

    id: int = Field(primary_key=True, auto_increment=True)
    status: _TestStrEnum = _TestStrEnum.PUBLISHED


class _DefaultPlainEnumModel(Model):
    class Meta:
        table = "default_plain_enum"

    id: int = Field(primary_key=True, auto_increment=True)
    flag: _TestPlainEnum = _TestPlainEnum.BAR


class _DefaultIntEnumModel(Model):
    class Meta:
        table = "default_int_enum"

    id: int = Field(primary_key=True, auto_increment=True)
    level: _TestIntEnum = _TestIntEnum.MEDIUM


def test_render_default_clause_str_enum():
    """StrEnum 默认值渲染为 DEFAULT 'value'。"""
    dialect = DDLDialect()
    field_info = _DefaultStrEnumModel.model_fields["status"]
    result = dialect._render_default_clause(field_info, "statusenum")
    assert result == "DEFAULT 'published'"


def test_render_default_clause_plain_enum():
    """非 StrEnum/IntEnum 的普通 Enum 也能正确渲染 DEFAULT。"""
    dialect = DDLDialect()
    field_info = _DefaultPlainEnumModel.model_fields["flag"]
    result = dialect._render_default_clause(field_info, "testenum")
    assert result == "DEFAULT 'bar'"


def test_render_default_clause_int_enum():
    """IntEnum 默认值渲染为 DEFAULT <number>。"""
    dialect = DDLDialect()
    field_info = _DefaultIntEnumModel.model_fields["level"]
    result = dialect._render_default_clause(field_info, "INTEGER")
    assert result == "DEFAULT 2"


# ---------------------------------------------------------------------------
# build_pre_create / introspect_enum_types — 基类默认行为
# ---------------------------------------------------------------------------


def test_build_pre_create_default_returns_empty():
    """build_pre_create 对无枚举模型返回空列表。"""

    class _NoEnumModel(Model):
        class Meta:
            table = "no_enum_test"

        id: int = Field(primary_key=True, auto_increment=True)
        name: str = Field(max_length=100)

    dialect = DDLDialect()
    result = dialect.build_pre_create(_NoEnumModel)
    assert result == []


async def test_introspect_enum_types_default_returns_empty():
    """基类 introspect_enum_types 默认返回空 dict。"""
    dialect = DDLDialect()
    result = await dialect.introspect_enum_types()
    assert result == {}


# ---------------------------------------------------------------------------
# PG 风格枚举 DDL 测试
# ---------------------------------------------------------------------------


class _PGEnumModel(Model):
    class Meta:
        table = "pg_enum_test"

    id: int = Field(primary_key=True, auto_increment=True)
    status: _TestStrEnum


class _PGMultiEnumModel(Model):
    class Meta:
        table = "pg_multi_enum"

    id: int = Field(primary_key=True, auto_increment=True)
    status: _TestStrEnum
    secondary: _TestStrEnum  # 同一个枚举类型用两次


class _PGDoubleEnumModel(Model):
    class Meta:
        table = "pg_double_enum"

    id: int = Field(primary_key=True, auto_increment=True)
    status: _TestStrEnum
    color: _TestPlainEnum  # 不同的枚举类型


def test_pg_map_enum_type_returns_lowercase_name():
    """PG map_python_type 对 enum 返回类名小写。"""
    dialect = DDLDialect()
    annotation = _PGEnumModel.model_fields["status"].annotation
    result = dialect.map_python_type(annotation, _PGEnumModel.__column_info__["status"])
    assert result == "_teststrenum"


def test_pg_map_enum_type_optional():
    """Optional[Enum] 也能正确映射。"""

    class _OptEnumModel(Model):
        class Meta:
            table = "opt"

        id: int = Field(primary_key=True, auto_increment=True)
        status: _TestStrEnum | None = None

    dialect = DDLDialect()
    annotation = _OptEnumModel.model_fields["status"].annotation
    result = dialect.map_python_type(annotation, _OptEnumModel.__column_info__["status"])
    assert result == "_teststrenum"


def test_pg_build_pre_create_single_enum():
    """生成 CREATE TYPE 语句。"""
    dialect = DDLDialect()
    result = dialect.build_pre_create(_PGEnumModel)
    assert len(result) == 1
    assert result[0] == """CREATE TYPE "_teststrenum" AS ENUM ('draft', 'published', 'archived')"""


def test_pg_build_pre_create_deduplicates_same_enum():
    """同一枚举类被多个字段引用时只生成一条 CREATE TYPE。"""
    dialect = DDLDialect()
    result = dialect.build_pre_create(_PGMultiEnumModel)
    assert len(result) == 1


def test_pg_build_pre_create_skips_existing_types():
    """已有类型跳过，不重复生成 CREATE TYPE。"""
    dialect = DDLDialect()
    result = dialect.build_pre_create(
        _PGEnumModel, existing_enums={"_teststrenum": {"draft", "published", "archived"}}
    )
    assert result == []


def test_pg_build_pre_create_multiple_enum_types():
    """多个不同枚举类型各自生成 CREATE TYPE。"""
    dialect = DDLDialect()
    result = dialect.build_pre_create(_PGDoubleEnumModel)
    assert len(result) == 2
    assert any("_teststrenum" in r for r in result)
    assert any("_testplainenum" in r for r in result)


def test_pg_build_pre_create_no_enum_model():
    """无枚举模型返回空列表。"""
    dialect = DDLDialect()

    class _NoEnumModel(Model):
        class Meta:
            table = "no_enum"

        id: int = Field(primary_key=True, auto_increment=True)
        name: str = Field(max_length=100)

    result = dialect.build_pre_create(_NoEnumModel)
    assert result == []


def test_pg_build_pre_alter_add_new_values():
    """已有枚举类型缺少新值 → ALTER TYPE ADD VALUE。"""
    dialect = DDLDialect()
    result = dialect.build_pre_alter(
        _PGEnumModel,
        existing_enums={"_teststrenum": {"draft"}},  # 缺少 published 和 archived
    )
    assert len(result) == 2
    assert """ALTER TYPE "_teststrenum" ADD VALUE 'archived'""" in result
    assert """ALTER TYPE "_teststrenum" ADD VALUE 'published'""" in result


def test_pg_build_pre_alter_no_change():
    """枚举值匹配时返回空列表。"""
    dialect = DDLDialect()
    result = dialect.build_pre_alter(
        _PGEnumModel,
        existing_enums={"_teststrenum": {"draft", "published", "archived"}},
    )
    assert result == []


def test_pg_build_pre_alter_new_type():
    """数据库中不存在该枚举类型 → 生成 CREATE TYPE。"""
    dialect = DDLDialect()
    result = dialect.build_pre_alter(_PGEnumModel, existing_enums={})
    assert len(result) == 1
    assert "CREATE TYPE" in result[0]


def test_pg_build_create_table_uses_enum_type_name():
    """建表语句中枚举列使用自定义类型名。"""
    dialect = DDLDialect()
    sql = dialect.build_create_table(_PGEnumModel)
    assert "_teststrenum" in sql


def test_pg_db_type_overrides_enum_auto_detect():
    """db_type 覆盖枚举自动检测。"""

    class _OverrideModel(Model):
        class Meta:
            table = "override"

        id: int = Field(primary_key=True, auto_increment=True)
        status: _TestStrEnum = Field(db_type="my_custom_enum")

    dialect = DDLDialect()
    annotation = _OverrideModel.model_fields["status"].annotation
    result = dialect.map_python_type(annotation, _OverrideModel.__column_info__["status"])
    assert result == "my_custom_enum"


# ---------------------------------------------------------------------------
# MySQL 风格 ENUM 测试
# ---------------------------------------------------------------------------


class _MySQLEnumModel(Model):
    class Meta:
        table = "mysql_enum_test"

    id: int = Field(primary_key=True, auto_increment=True)
    status: _TestStrEnum


@pytest.fixture
def mysql_dialect_enum() -> MySQLTestDialect:
    return MySQLTestDialect()


def test_mysql_map_enum_type_returns_enum_syntax():
    """MySQL map_python_type 对 enum 返回 ENUM('v1','v2',...)。"""
    dialect = MySQLTestDialect()
    annotation = _MySQLEnumModel.model_fields["status"].annotation
    result = dialect.map_python_type(annotation, _MySQLEnumModel.__column_info__["status"])
    assert result == "ENUM('draft','published','archived')"


def test_mysql_enum_db_type_override():
    """MySQL db_type 覆盖枚举自动检测。"""

    class _MySQLOverrideModel(Model):
        class Meta:
            table = "mysql_override"

        id: int = Field(primary_key=True, auto_increment=True)
        status: _TestStrEnum = Field(db_type="ENUM('draft','published')")

    dialect = MySQLTestDialect()
    annotation = _MySQLOverrideModel.model_fields["status"].annotation
    result = dialect.map_python_type(annotation, _MySQLOverrideModel.__column_info__["status"])
    assert result == "ENUM('draft','published')"


def test_mysql_create_table_includes_inline_enum():
    """MySQL 建表语句中枚举列包含内联 ENUM 定义。"""
    dialect = MySQLTestDialect()
    sql = dialect.build_create_table(_MySQLEnumModel)
    assert "ENUM('draft','published','archived')" in sql


def test_mysql_int_enum_maps_to_int():
    """MySQL IntEnum 映射为 INT（不生成 ENUM，因为 int 分支先匹配）。"""
    dialect = MySQLTestDialect()
    annotation = _DefaultIntEnumModel.model_fields["level"].annotation
    result = dialect.map_python_type(annotation, _DefaultIntEnumModel.__column_info__["level"])
    assert result == "INT"


def test_mysql_modify_columns_detects_enum_value_change():
    """MySQL MODIFY COLUMN：枚举值变更时触发 MODIFY COLUMN。"""
    dialect = MySQLTestDialect()

    class _OldEnumModel(Model):
        class Meta:
            table = "old_enum"

        id: int = Field(primary_key=True, auto_increment=True)
        status: _TestStrEnum  # 完整值：draft, published, archived

    # 模拟数据库中的旧值只有 2 个
    existing = IntrospectedColumn(
        column_name="status",
        data_type="enum('draft','published')",
        is_nullable=True,
    )
    result = dialect.build_modify_columns(_OldEnumModel, [("status", existing)])
    # 类型从 enum('draft','published') 变为 ENUM('draft','published','archived')
    # lower 后不匹配 → type_changed=True → MODIFY COLUMN
    assert "MODIFY COLUMN" in result
    assert "ENUM" in result


def test_mysql_modify_columns_skips_when_enum_unchanged():
    """MySQL MODIFY COLUMN：枚举值不变时跳过。"""
    dialect = MySQLTestDialect()

    class _SameEnumModel(Model):
        class Meta:
            table = "same_enum"

        id: int = Field(primary_key=True, auto_increment=True)
        status: _TestStrEnum

    # 模拟数据库中已有完整值
    existing = IntrospectedColumn(
        column_name="status",
        data_type="enum('draft','published','archived')",
        is_nullable=True,
    )
    result = dialect.build_modify_columns(_SameEnumModel, [("status", existing)])
    assert result == ""


# ---------------------------------------------------------------------------
# sync_table 端到端枚举测试
# ---------------------------------------------------------------------------


class _SyncEnumModel(Model):
    class Meta:
        table = "sync_enum_test"

    id: int = Field(primary_key=True, auto_increment=True)
    status: _TestStrEnum


async def test_sync_table_creates_with_enum_type():
    """建表时 pre_create 先于 create_table 执行。"""
    dialect = RecordingDDLDialect(
        introspect_result={},  # 表不存在
    )
    session = Session(dialect)

    await _SyncEnumModel.sync_table(session.dialect)

    # 验证 pre_create 在 create_table 之前执行
    assert len(dialect.executed_sqls) == 2
    assert "CREATE TYPE" in dialect.executed_sqls[0]
    assert "CREATE TABLE" in dialect.executed_sqls[1]


async def test_sync_table_skips_existing_enum_type():
    """已有枚举类型时不重复 CREATE TYPE。"""
    dialect = RecordingDDLDialect(
        introspect_result={},  # 表不存在
        introspect_enums={"_teststrenum": {"draft", "published", "archived"}},
    )
    session = Session(dialect)

    await _SyncEnumModel.sync_table(session.dialect)

    # pre_create 为空，只有 CREATE TABLE
    assert len(dialect.executed_sqls) == 1
    assert "CREATE TABLE" in dialect.executed_sqls[0]


async def test_sync_table_adds_new_enum_value_without_column_change():
    """表已存在、列类型不变，仅枚举新增了值时，仍应执行 ALTER TYPE ADD VALUE。"""
    existing = {
        "id": IntrospectedColumn("id", "serial", is_nullable=False),
        "status": IntrospectedColumn("status", "_teststrenum", is_nullable=True),
    }
    dialect = RecordingDDLDialect(
        introspect_result=existing,
        introspect_enums={"_teststrenum": {"draft"}},  # DB 缺少 published / archived
    )
    session = Session(dialect)

    await _SyncEnumModel.sync_table(session.dialect)

    alter_type_sqls = [s for s in dialect.executed_sqls if "ALTER TYPE" in s]
    assert len(alter_type_sqls) == 2  # published + archived
