from __future__ import annotations

import datetime
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

    async def introspect_indexes(self, table_name: str) -> set[str]:
        return set()

    async def introspect_columns(self, table_name: str) -> dict[str, IntrospectedColumn]:
        return {}


class RecordingDDLDialect(DDLDialect):
    """记录所有执行的 SQL，并可预设内省结果。"""

    def __init__(
        self,
        introspect_result: dict[str, IntrospectedColumn] | None = None,
        introspect_indexes: set[str] | None = None,
    ) -> None:
        self._introspect_result = introspect_result or {}
        self._introspect_indexes = introspect_indexes or set()
        self.executed_sqls: list[str] = []
        self.last_sql: str = ""
        self.last_params: list[Any] = []

    async def execute(self, sql: str, params: list[Any]) -> Any:
        self.last_sql = sql
        self.last_params = params
        self.executed_sqls.append(sql)
        return None

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
