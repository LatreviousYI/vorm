from __future__ import annotations

from typing import Any

import pytest

from vorm import Field, Model, Session, col
from vorm.dialects.base import AbstractDialect


class DummyDialect(AbstractDialect):
    def render_placeholder(self, index: int) -> str:
        return f"${index}"

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
        return None

    def render_json_extract(self, column_sql: str, path: str, as_text: bool) -> str:
        op = "->>" if as_text else "->"
        return f"{column_sql}{op}'{path}'"

    def map_python_type(self, annotation: Any, column_info: Any) -> str:
        return "INTEGER"

    async def introspect_indexes(self, table_name: str) -> set[str]:
        return set()

    async def introspect_columns(self, table_name: str) -> dict[str, Any]:
        return {}


class User(Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto_increment=True)
    name: str
    age: int | None = None


@pytest.fixture
def session() -> Session:
    return Session(DummyDialect())


def test_build_select_with_filters_order_limit_offset(session: Session) -> None:
    query = (
        session.query(User)
        .filter(User.age > 18, User.name != "Bob")
        .order_by(User.name.desc())
        .limit(10)
        .offset(20)
    )

    sql, params = query.build_sql()

    assert sql == (
        'SELECT "users"."id", "users"."name", "users"."age" FROM "users" '
        'WHERE "users"."age" > $1 AND "users"."name" != $2 '
        'ORDER BY "users"."name" DESC LIMIT $3 OFFSET $4'
    )
    assert params == [18, "Bob", 10, 20]


def test_order_by_desc_col(session: Session) -> None:
    """col() 返回 Column，链式 .desc() 与直接 column.desc() 等价。"""
    query = session.query(User).order_by(col(User.name).desc())

    sql, _ = query.build_sql()

    assert 'ORDER BY "users"."name" DESC' in sql


def test_order_by_asc_col(session: Session) -> None:
    """col() 返回 Column，链式 .asc() 与直接 column.asc() 等价。"""
    query = session.query(User).order_by(col(User.name).asc())

    sql, _ = query.build_sql()

    assert 'ORDER BY "users"."name" ASC' in sql


def test_build_insert_skips_empty_auto_increment_pk(session: Session) -> None:
    user = User(name="Alice", age=25)

    sql, params = session.dialect.build_insert(user)

    assert sql == 'INSERT INTO "users" ("name", "age") VALUES ($1, $2)'
    assert params == ["Alice", 25]


def test_build_update_uses_primary_key_in_where(session: Session) -> None:
    user = User(id=1, name="Alice", age=26)

    sql, params = session.dialect.build_update(user)

    assert sql == 'UPDATE "users" SET "name" = $1, "age" = $2 WHERE "users"."id" = $3'
    assert params == ["Alice", 26, 1]


def test_in_empty_renders_false_condition(session: Session) -> None:
    sql, params = session.query(User).filter(User.id.in_([])).build_sql()

    assert sql == 'SELECT "users"."id", "users"."name", "users"."age" FROM "users" WHERE 1 = 0'
    assert params == []


# ---------------------------------------------------------------------------
# 批量 INSERT
# ---------------------------------------------------------------------------


def test_build_bulk_insert(session: Session) -> None:
    users = [
        User(name="Alice", age=25),
        User(name="Bob", age=30),
        User(name="Charlie", age=None),
    ]

    sql, params = session.dialect.build_bulk_insert(users)

    assert sql == ('INSERT INTO "users" ("name", "age") VALUES ($1, $2), ($3, $4), ($5, $6)')
    assert params == ["Alice", 25, "Bob", 30, "Charlie", None]


def test_build_bulk_insert_empty_raises(session: Session) -> None:
    with pytest.raises(ValueError, match="at least one instance"):
        session.dialect.build_bulk_insert([])


# ---------------------------------------------------------------------------
# 批量 UPDATE (QuerySet.update)
# ---------------------------------------------------------------------------


def test_build_update_by_query(session: Session) -> None:
    query = session.query(User).filter(User.age.is_null())

    sql, params = session.dialect.build_update_by_query(query, {"age": 0, "name": "unknown"})

    assert sql == ('UPDATE "users" SET "age" = $1, "name" = $2 WHERE "users"."age" IS NULL')
    assert params == [0, "unknown"]


def test_build_update_by_query_no_filter(session: Session) -> None:
    """无 filter 时更新全表（危险操作，由调用方控制）。"""
    query = session.query(User)

    sql, params = session.dialect.build_update_by_query(query, {"age": 0})

    assert sql == 'UPDATE "users" SET "age" = $1'
    assert params == [0]


# ---------------------------------------------------------------------------
# 批量 DELETE (QuerySet.delete)
# ---------------------------------------------------------------------------


def test_build_delete_by_query(session: Session) -> None:
    query = session.query(User).filter(User.age < 18)

    sql, params = session.dialect.build_delete_by_query(query)

    assert sql == 'DELETE FROM "users" WHERE "users"."age" < $1'
    assert params == [18]


def test_build_delete_by_query_no_filter(session: Session) -> None:
    """无 filter 时删除全表（危险操作，由调用方控制）。"""
    query = session.query(User)

    sql, params = session.dialect.build_delete_by_query(query)

    assert sql == 'DELETE FROM "users"'
    assert params == []


# ---------------------------------------------------------------------------
# PG INSERT ... RETURNING
# ---------------------------------------------------------------------------


def test_pg_build_insert_includes_returning() -> None:
    """PostgreSQL 的 build_insert 应在语句末尾追加 RETURNING 主键列。"""
    from vorm.dialects.postgresql import PostgreSQLDialect

    dialect = PostgreSQLDialect(pool=None)  # type: ignore[arg-type]  # 仅测 SQL 生成，不连库
    user = User(name="Alice", age=25)

    sql, params = dialect.build_insert(user)

    assert sql == ('INSERT INTO "users" ("name", "age") VALUES ($1, $2) RETURNING "id"')
    assert params == ["Alice", 25]


# ---------------------------------------------------------------------------
# execute_insert（验证 INSERT 后回填 PK）
# ---------------------------------------------------------------------------


class RecordingDialect(AbstractDialect):
    """记录 SQL 调用，可配置 execute_insert 返回值。"""

    def __init__(self, insert_result: Any = None) -> None:
        self.insert_result = insert_result
        self.last_sql: str = ""
        self.last_params: list[Any] = []

    def render_placeholder(self, index: int) -> str:
        return f"${index}"

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
        self.last_sql = sql
        self.last_params = params
        return self.insert_result

    async def fetch(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        return []

    async def fetchrow(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        return None

    async def close(self) -> None:
        pass

    def render_json_extract(self, column_sql: str, path: str, as_text: bool) -> str:
        op = "->>" if as_text else "->"
        return f"{column_sql}{op}'{path}'"

    def map_python_type(self, annotation: Any, column_info: Any) -> str:
        return "INTEGER"

    async def introspect_indexes(self, table_name: str) -> set[str]:
        return set()

    async def introspect_columns(self, table_name: str) -> dict[str, Any]:
        return {}


async def test_save_insert_calls_execute_insert_and_backfills_pk() -> None:
    """session.save() 对无 PK 实例调用 execute_insert，并用返回值回填 PK。"""
    dialect = RecordingDialect(insert_result=42)
    session = Session(dialect)

    user = User(name="Alice", age=25)
    assert user.id is None

    await session.save(user)

    assert "INSERT" in dialect.last_sql
    assert user.id == 42


async def test_save_update_does_not_call_execute_insert() -> None:
    """session.save() 对已有 PK 实例调用 execute（UPDATE），不调 execute_insert。"""
    dialect = RecordingDialect(insert_result=99)
    session = Session(dialect)

    user = User(id=1, name="Alice", age=25)
    await session.save(user)

    # update 走 execute 而不是 execute_insert
    assert "UPDATE" in dialect.last_sql
    assert user.id == 1  # 不应被覆盖


# ---------------------------------------------------------------------------
# JOIN — 多表查询
# ---------------------------------------------------------------------------

# JOIN 测试用的额外模型


class Post(Model):
    class Meta:
        table = "posts"

    id: int = Field(primary_key=True, auto_increment=True)
    user_id: int
    title: str


class Comment(Model):
    class Meta:
        table = "comments"

    id: int = Field(primary_key=True, auto_increment=True)
    post_id: int
    body: str


def test_join_single_join_generates_correct_sql(session: Session) -> None:
    """单个 INNER JOIN 应产生正确的 SELECT + JOIN 语句。"""
    sql, params = session.query(User).join(Post, on=User.id == Post.user_id).build_sql()

    # aliases "users_id", "posts_id" 需要转义
    assert sql == (
        'SELECT "users"."id" AS "users_id", "users"."name" AS "users_name", '
        '"users"."age" AS "users_age", '
        '"posts"."id" AS "posts_id", "posts"."user_id" AS "posts_user_id", '
        '"posts"."title" AS "posts_title" '
        'FROM "users" '
        'INNER JOIN "posts" ON "users"."id" = "posts"."user_id"'
    )
    assert params == []


def test_join_two_joins_three_tables(session: Session) -> None:
    """三表 JOIN（User → Post → Comment）。"""
    sql, params = (
        session.query(User)
        .join(Post, on=User.id == Post.user_id)
        .join(Comment, on=Post.id == Comment.post_id)
        .build_sql()
    )

    assert "FROM" in sql
    assert 'INNER JOIN "posts" ON "users"."id" = "posts"."user_id"' in sql
    assert 'INNER JOIN "comments" ON "posts"."id" = "comments"."post_id"' in sql
    assert '"comments"."id" AS "comments_id"' in sql
    assert '"comments"."body" AS "comments_body"' in sql
    assert params == []


def test_join_with_filter_and_order(session: Session) -> None:
    """JOIN + filter + order_by 的组合。"""
    sql, params = (
        session.query(User)
        .join(Post, on=User.id == Post.user_id)
        .filter(Post.title == "Hello")
        .order_by(User.name.asc())
        .build_sql()
    )

    assert 'INNER JOIN "posts" ON "users"."id" = "posts"."user_id"' in sql
    assert 'WHERE "posts"."title" = $1' in sql
    assert 'ORDER BY "users"."name" ASC' in sql
    assert params == ["Hello"]


def test_join_left_join(session: Session) -> None:
    """LEFT JOIN 应在 SQL 中出现 LEFT JOIN 关键字。"""
    sql, params = (
        session.query(User).join(Post, on=User.id == Post.user_id, type="LEFT").build_sql()
    )

    assert 'LEFT JOIN "posts" ON "users"."id" = "posts"."user_id"' in sql
    assert params == []


def test_join_count(session: Session) -> None:
    """带 JOIN 的 COUNT 应包含 JOIN 子句。"""
    sql, params = (
        session.query(User)
        .join(Post, on=User.id == Post.user_id)
        .filter(Post.title == "X")
        .build_sql()
    )
    # count 通过 build_select(count=True) 调用，模拟一下
    sql_count, _ = session.dialect.build_select(
        session.query(User).join(Post, on=User.id == Post.user_id).filter(Post.title == "X"),
        count=True,
    )

    assert "COUNT(*) AS count" in sql_count
    assert 'INNER JOIN "posts"' in sql_count
    assert 'WHERE "posts"."title" = $1' in sql_count


# ---------------------------------------------------------------------------
# LIKE / BETWEEN 表达式
# ---------------------------------------------------------------------------


def test_like_expression(session: Session) -> None:
    """Column.like() 应生成 LIKE 表达式。"""
    sql, params = session.query(User).filter(User.name.like("A%")).build_sql()

    assert '"users"."name" LIKE $1' in sql
    assert params == ["A%"]


def test_like_with_multiple_filters(session: Session) -> None:
    """LIKE 可以与其他 filter 组合使用。"""
    sql, params = session.query(User).filter(User.name.like("%lice%"), User.age > 18).build_sql()

    assert "LIKE $1" in sql
    assert ">" in sql
    assert params == ["%lice%", 18]


def test_between_expression(session: Session) -> None:
    """Column.between() 应生成 BETWEEN ... AND ... 表达式。"""
    sql, params = session.query(User).filter(User.age.between(18, 65)).build_sql()

    assert '"users"."age" BETWEEN $1 AND $2' in sql
    assert params == [18, 65]


def test_between_with_order(session: Session) -> None:
    """BETWEEN 可以与 ORDER BY 组合。"""
    sql, params = (
        session.query(User).filter(User.age.between(20, 30)).order_by(User.name.asc()).build_sql()
    )

    assert "BETWEEN $1 AND $2" in sql
    assert 'ORDER BY "users"."name" ASC' in sql
    assert params == [20, 30]


def test_like_and_between_combined(session: Session) -> None:
    """LIKE 和 BETWEEN 可以同时使用。"""
    sql, params = (
        session.query(User).filter(User.name.like("A%"), User.age.between(25, 50)).build_sql()
    )

    assert "LIKE $1" in sql
    assert "BETWEEN $2 AND $3" in sql
    assert params == ["A%", 25, 50]


# ---------------------------------------------------------------------------
# JSON 路径查询
# ---------------------------------------------------------------------------


class Product(Model):
    """JSON 列测试模型。"""

    class Meta:
        table = "products"

    id: int = Field(primary_key=True, auto_increment=True)
    data: dict  # → JSON/JSONB


def test_json_path_eq(session: Session) -> None:
    """json_path + == 应生成 column->>'$.key' = $1 参数化查询。"""
    sql, params = (
        session.query(Product).filter(Product.data.json_path("$.name") == "Alice").build_sql()
    )

    assert "->>" in sql
    assert "'$.name'" in sql
    assert "= $1" in sql
    assert params == ["Alice"]


def test_json_path_gt(session: Session) -> None:
    """json_path 比较运算符应生效。"""
    sql, params = (
        session.query(Product)
        .filter(Product.data.json_path("$.price", as_text=False) > 100)
        .build_sql()
    )

    assert "->" in sql
    assert "->>" not in sql  # as_text=False 用 -> 而非 ->>
    assert "> $1" in sql
    assert params == [100]


def test_json_path_multiple_filters(session: Session) -> None:
    """json_path 可以与其他 filter 组合。"""
    sql, params = (
        session.query(Product)
        .filter(
            Product.data.json_path("$.category") == "book",
            Product.data.json_path("$.price") > 50,
        )
        .build_sql()
    )

    assert "WHERE" in sql
    assert "AND" in sql
    assert params == ["book", 50]


# ---------------------------------------------------------------------------
# timestamp_behavior — 自动时间戳
# ---------------------------------------------------------------------------


class TimestampedModel(Model):
    """测试 timestamp_behavior 的模型。"""

    class Meta:
        table = "timestamped"

    id: int = Field(primary_key=True, auto_increment=True)
    title: str
    created_at: str = Field(timestamp_behavior="create")
    updated_at: str = Field(timestamp_behavior="both")


def test_build_update_skips_create_timestamp(session: Session) -> None:
    """build_update 应跳过 timestamp_behavior='create' 的字段。"""
    instance = TimestampedModel(id=1, title="test", created_at="old", updated_at="old")

    sql, params = session.dialect.build_update(instance)

    # created_at 不应出现在 SET 子句中
    assert "created_at" not in sql
    # updated_at 应出现
    assert "updated_at" in sql
    assert "title" in sql


def test_build_update_recomputes_both_timestamp(session: Session) -> None:
    """build_update 对 timestamp_behavior='both' 的字段应重新计算（不用实例旧值）。"""
    instance = TimestampedModel(id=1, title="test", created_at="old", updated_at="old_ignored")

    sql, params = session.dialect.build_update(instance)

    # updated_at 的新值不应是旧值 "old_ignored"
    assert "old_ignored" not in params


def test_build_update_by_query_auto_injects_timestamp(session: Session) -> None:
    """build_update_by_query 应自动为 'both' 字段注入当前时间戳。"""
    query = session.query(TimestampedModel).filter(TimestampedModel.title == "test")

    sql, params = session.dialect.build_update_by_query(query, {"title": "updated"})

    # updated_at 应自动出现在 SET 子句中
    assert "updated_at" in sql
    # created_at（'create' 行为）不应自动出现
    assert "created_at" not in sql
    assert "title" in sql


def test_build_insert_includes_all_timestamps(session: Session) -> None:
    """build_insert 应正常包含所有字段（timestamp_behavior 不影响 INSERT）。"""
    instance = TimestampedModel(title="new", created_at="now", updated_at="now")

    sql, params = session.dialect.build_insert(instance)

    assert "created_at" in sql
    assert "updated_at" in sql
    assert "title" in sql


# ---------------------------------------------------------------------------
# select() — 字段选择 + 别名
# ---------------------------------------------------------------------------


def test_select_columns_basic(session: Session) -> None:
    """select() 指定列时应只生成对应列，不含其他列。"""
    sql, params = session.query(User).select(User.id, User.name).build_sql()

    assert '"users"."id"' in sql
    assert '"users"."name"' in sql
    # age 不应出现
    assert '"users"."age"' not in sql
    assert params == []


def test_select_columns_with_alias(session: Session) -> None:
    """select() 加 alias 应生成 AS 子句。"""
    sql, params = (
        session.query(User)
        .select(User.id.alias("user_id"), User.name.alias("user_name"))
        .build_sql()
    )

    assert '"users"."id" AS "user_id"' in sql
    assert '"users"."name" AS "user_name"' in sql
    assert params == []


def test_select_with_filter(session: Session) -> None:
    """select() 可以与 filter 组合使用。"""
    sql, params = session.query(User).select(User.id, User.name).filter(User.age > 18).build_sql()

    assert '"users"."id"' in sql
    assert '"users"."name"' in sql
    assert ">" in sql
    assert params == [18]


def test_select_with_join_and_alias(session: Session) -> None:
    """select() + join + alias 应生成自定义列名的 JOIN 查询。"""

    class Post(Model):
        class Meta:
            table = "posts"

        id: int = Field(primary_key=True, auto_increment=True)
        user_id: int
        title: str

    sql, params = (
        session.query(User)
        .join(Post, on=User.id == Post.user_id)
        .select(
            User.id.alias("user_id"),
            User.name.alias("user_name"),
            Post.title.alias("post_title"),
        )
        .build_sql()
    )

    assert '"users"."id" AS "user_id"' in sql
    assert '"users"."name" AS "user_name"' in sql
    assert '"posts"."title" AS "post_title"' in sql
    assert 'INNER JOIN "posts"' in sql
    assert params == []
