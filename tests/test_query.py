from __future__ import annotations

from typing import Any

import pytest

from eorm import Field, Model, Session
from eorm.dialects.base import AbstractDialect
from eorm.exceptions import DoesNotExist, MultipleObjectsReturned


class ConfigurableDialect(AbstractDialect):
    """A dialect that returns pre-configured data for testing query execution."""

    def __init__(
        self,
        fetch_rows: list[dict[str, Any]] | None = None,
        fetchrow_result: dict[str, Any] | None = None,
        execute_result: Any = None,
    ) -> None:
        self.fetch_rows = fetch_rows or []
        self.fetchrow_result = fetchrow_result
        self.execute_result = execute_result
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
        return self.execute_result

    async def execute_insert(self, sql: str, params: list[Any]) -> Any:
        self.last_sql = sql
        self.last_params = params
        return self.execute_result

    async def fetch(self, sql: str, params: list[Any]) -> list[dict[str, Any]]:
        self.last_sql = sql
        self.last_params = params
        return self.fetch_rows

    async def fetchrow(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
        self.last_sql = sql
        self.last_params = params
        return self.fetchrow_result

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


class User(Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto_increment=True)
    name: str
    email: str
    age: int | None = None


@pytest.fixture
def session() -> Session:
    return Session(ConfigurableDialect())


@pytest.fixture
def dialect(session: Session) -> ConfigurableDialect:
    return session.dialect  # type: ignore[return-value]


@pytest.fixture
def sample_rows() -> list[dict[str, Any]]:
    return [
        {"id": 1, "name": "Alice", "email": "alice@example.com", "age": 30},
        {"id": 2, "name": "Bob", "email": "bob@example.com", "age": 25},
        {"id": 3, "name": "Charlie", "email": "charlie@example.com", "age": None},
    ]


# -- .all() ----------------------------------------------------------------


async def test_all_returns_model_instances(
    session: Session, dialect: ConfigurableDialect, sample_rows: list[dict[str, Any]]
) -> None:
    """query.all() hydrates rows into model instances via model_validate."""
    dialect.fetch_rows = sample_rows

    users = await session.query(User).all()

    assert len(users) == 3
    assert all(isinstance(u, User) for u in users)
    assert users[0].id == 1
    assert users[0].name == "Alice"
    assert users[0].email == "alice@example.com"
    assert users[0].age == 30
    assert users[1].name == "Bob"
    assert users[2].name == "Charlie"
    assert users[2].age is None


async def test_all_empty_result(session: Session, dialect: ConfigurableDialect) -> None:
    """query.all() returns an empty list when no rows match."""
    dialect.fetch_rows = []

    users = await session.query(User).all()

    assert users == []


async def test_all_with_filter(session: Session, dialect: ConfigurableDialect) -> None:
    """Filters are passed through to the generated SQL."""
    dialect.fetch_rows = [{"id": 1, "name": "Alice", "email": "alice@example.com", "age": 30}]

    users = await session.query(User).filter(User.name == "Alice").all()

    assert len(users) == 1
    assert "WHERE" in dialect.last_sql
    assert "Alice" in dialect.last_params


# -- .first() --------------------------------------------------------------


async def test_first_returns_first_row(session: Session, dialect: ConfigurableDialect) -> None:
    """query.first() returns the first matching row as a model instance."""
    dialect.fetch_rows = [
        {"id": 1, "name": "Alice", "email": "alice@example.com", "age": 30},
    ]

    user = await session.query(User).first()

    assert user is not None
    assert isinstance(user, User)
    assert user.id == 1
    assert user.name == "Alice"
    # .first() implicitly calls .limit(1), so LIMIT should be in the SQL
    assert "LIMIT" in dialect.last_sql


async def test_first_returns_none_when_empty(
    session: Session, dialect: ConfigurableDialect
) -> None:
    """query.first() returns None when no rows match."""
    dialect.fetch_rows = []

    user = await session.query(User).first()

    assert user is None


# -- .one() ----------------------------------------------------------------


async def test_one_returns_single_row(session: Session, dialect: ConfigurableDialect) -> None:
    """query.one() returns exactly one model instance when one row matches."""
    dialect.fetch_rows = [
        {"id": 1, "name": "Alice", "email": "alice@example.com", "age": 30},
    ]

    user = await session.query(User).filter(User.id == 1).one()

    assert isinstance(user, User)
    assert user.name == "Alice"


async def test_one_raises_does_not_exist_when_no_rows(
    session: Session, dialect: ConfigurableDialect
) -> None:
    """query.one() raises DoesNotExist when no rows match."""
    dialect.fetch_rows = []

    with pytest.raises(DoesNotExist, match="No User row matched the query"):
        await session.query(User).filter(User.id == 999).one()


async def test_one_raises_multiple_objects_returned_when_many_rows(
    session: Session, dialect: ConfigurableDialect, sample_rows: list[dict[str, Any]]
) -> None:
    """query.one() raises MultipleObjectsReturned when more than one row matches."""
    dialect.fetch_rows = sample_rows  # 3 rows

    with pytest.raises(MultipleObjectsReturned, match="Multiple User rows matched the query"):
        await session.query(User).one()


# -- .count() --------------------------------------------------------------


async def test_count_returns_row_count(session: Session, dialect: ConfigurableDialect) -> None:
    """query.count() returns the number of matching rows."""
    dialect.fetchrow_result = {"count": 42}

    total = await session.query(User).count()

    assert total == 42
    assert "COUNT(*)" in dialect.last_sql


async def test_count_with_filter(session: Session, dialect: ConfigurableDialect) -> None:
    """Filters affect the COUNT query."""
    dialect.fetchrow_result = {"count": 2}

    total = await session.query(User).filter(User.age > 18).count()

    assert total == 2
    assert "WHERE" in dialect.last_sql


async def test_count_returns_zero_when_fetchrow_is_none(
    session: Session, dialect: ConfigurableDialect
) -> None:
    """query.count() returns 0 when the dialect returns None."""
    dialect.fetchrow_result = None

    total = await session.query(User).count()

    assert total == 0


# -- .exists() -------------------------------------------------------------


async def test_exists_returns_true(session: Session, dialect: ConfigurableDialect) -> None:
    """query.exists() returns True when at least one row matches."""
    dialect.fetchrow_result = {"count": 1}

    result = await session.query(User).filter(User.name == "Alice").exists()

    assert result is True


async def test_exists_returns_false(session: Session, dialect: ConfigurableDialect) -> None:
    """query.exists() returns False when no rows match."""
    dialect.fetchrow_result = {"count": 0}

    result = await session.query(User).filter(User.name == "Nobody").exists()

    assert result is False


# -- Chained query ---------------------------------------------------------


async def test_chained_query_all_methods(session: Session, dialect: ConfigurableDialect) -> None:
    """Demonstrates a fully chained query: filter + order_by + limit + offset."""
    dialect.fetch_rows = [
        {"id": 3, "name": "Charlie", "email": "charlie@example.com", "age": None},
        {"id": 1, "name": "Alice", "email": "alice@example.com", "age": 30},
    ]

    users = (
        await session.query(User)
        .filter(User.age >= 18)
        .order_by(User.name.asc())
        .limit(10)
        .offset(0)
        .all()
    )

    assert len(users) == 2
    assert users[0].name == "Charlie"
    assert users[1].name == "Alice"
    # Verify SQL contains ordering, limit, and offset
    assert "ORDER BY" in dialect.last_sql
    assert "LIMIT" in dialect.last_sql
    assert "OFFSET" in dialect.last_sql


# ---------------------------------------------------------------------------
# JOIN 执行测试
# ---------------------------------------------------------------------------


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


@pytest.fixture
def join_rows() -> list[dict[str, Any]]:
    """模拟 User JOIN Post 返回的原始行（列名带表前缀）。"""
    return [
        {
            "users_id": 1,
            "users_name": "Alice",
            "users_email": "alice@e.com",
            "users_age": 30,
            "posts_id": 101,
            "posts_user_id": 1,
            "posts_title": "Hello",
        },
        {
            "users_id": 1,
            "users_name": "Alice",
            "users_email": "alice@e.com",
            "users_age": 30,
            "posts_id": 102,
            "posts_user_id": 1,
            "posts_title": "World",
        },
        {
            "users_id": 2,
            "users_name": "Bob",
            "users_email": "bob@e.com",
            "users_age": 25,
            "posts_id": 103,
            "posts_user_id": 2,
            "posts_title": "Hi",
        },
    ]


async def test_join_all_returns_row_objects(
    session: Session, dialect: ConfigurableDialect, join_rows: list[dict[str, Any]]
) -> None:
    """带 JOIN 的查询 .all() 应返回 Row 对象，而非 Model 实例。"""
    from eorm.row import Row

    dialect.fetch_rows = join_rows

    rows = await session.query(User).join(Post, on=User.id == Post.user_id).all()

    assert len(rows) == 3
    assert all(isinstance(r, Row) for r in rows)


async def test_join_row_table_attr_access(
    session: Session, dialect: ConfigurableDialect, join_rows: list[dict[str, Any]]
) -> None:
    """Row 对象支持 row.users.name 形式的属性访问。"""
    from eorm.row import RowSnapshot

    dialect.fetch_rows = join_rows

    rows = await session.query(User).join(Post, on=User.id == Post.user_id).all()

    # row.users 返回 RowSnapshot
    assert isinstance(rows[0].users, RowSnapshot)
    assert rows[0].users.name == "Alice"
    assert rows[0].users.id == 1

    # row.posts 同样
    assert isinstance(rows[0].posts, RowSnapshot)
    assert rows[0].posts.title == "Hello"
    assert rows[0].posts.id == 101


async def test_join_row_bracket_access(
    session: Session, dialect: ConfigurableDialect, join_rows: list[dict[str, Any]]
) -> None:
    """Row 支持 row["users.id"] 和 row["users_id"] 两种字符串访问。"""
    dialect.fetch_rows = join_rows

    rows = await session.query(User).join(Post, on=User.id == Post.user_id).all()

    assert rows[0]["users.id"] == 1
    assert rows[0]["users_name"] == "Alice"
    assert rows[0]["posts.title"] == "Hello"


async def test_join_row_dict_conversion(
    session: Session, dialect: ConfigurableDialect, join_rows: list[dict[str, Any]]
) -> None:
    """dict(row) 返回扁平字典。"""
    dialect.fetch_rows = join_rows

    rows = await session.query(User).join(Post, on=User.id == Post.user_id).all()

    d = dict(rows[0])
    assert d["users_id"] == 1
    assert d["users_name"] == "Alice"
    assert d["posts_title"] == "Hello"


async def test_join_first(
    session: Session, dialect: ConfigurableDialect, join_rows: list[dict[str, Any]]
) -> None:
    """.first() 配合 JOIN 应返回单个 Row。"""
    from eorm.row import Row

    dialect.fetch_rows = [join_rows[0]]

    row = await session.query(User).join(Post, on=User.id == Post.user_id).first()

    assert isinstance(row, Row)
    assert row.users.name == "Alice"
    assert "LIMIT" in dialect.last_sql


async def test_join_one(
    session: Session, dialect: ConfigurableDialect, join_rows: list[dict[str, Any]]
) -> None:
    """.one() 配合 JOIN 应返回单个 Row。"""
    from eorm.row import Row

    dialect.fetch_rows = [join_rows[0]]

    row = await session.query(User).join(Post, on=User.id == Post.user_id).one()

    assert isinstance(row, Row)
    assert row.users.name == "Alice"


async def test_join_three_tables(session: Session, dialect: ConfigurableDialect) -> None:
    """三表 JOIN 的 Row 应包含三张表的字段。"""
    from eorm.row import Row, RowSnapshot

    dialect.fetch_rows = [
        {
            "users_id": 1,
            "users_name": "Alice",
            "users_email": "a@e.com",
            "users_age": 30,
            "posts_id": 101,
            "posts_user_id": 1,
            "posts_title": "Hello",
            "comments_id": 201,
            "comments_post_id": 101,
            "comments_body": "Nice!",
        }
    ]

    rows = (
        await session.query(User)
        .join(Post, on=User.id == Post.user_id)
        .join(Comment, on=Post.id == Comment.post_id)
        .all()
    )

    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, Row)
    assert isinstance(row.users, RowSnapshot)
    assert isinstance(row.posts, RowSnapshot)
    assert isinstance(row.comments, RowSnapshot)
    assert row.users.name == "Alice"
    assert row.posts.title == "Hello"
    assert row.comments.body == "Nice!"


async def test_single_table_still_returns_model(
    session: Session, dialect: ConfigurableDialect, sample_rows: list[dict[str, Any]]
) -> None:
    """无 JOIN 的查询仍应返回 Model 实例（回归验证）。"""
    dialect.fetch_rows = sample_rows

    users = await session.query(User).all()

    assert len(users) == 3
    assert all(isinstance(u, User) for u in users)
    assert isinstance(users[0].name, str)  # 是真实值，不是 RowSnapshot


# ---------------------------------------------------------------------------
# LIKE / BETWEEN 执行测试
# ---------------------------------------------------------------------------


async def test_filter_like(session: Session, dialect: ConfigurableDialect) -> None:
    """filter(User.name.like(...)) 应在 SQL 中包含 LIKE。"""
    dialect.fetch_rows = [
        {"id": 1, "name": "Alice", "email": "alice@e.com", "age": 30},
    ]

    users = await session.query(User).filter(User.name.like("A%")).all()

    assert len(users) == 1
    assert "LIKE" in dialect.last_sql
    assert "A%" in dialect.last_params


async def test_filter_between(session: Session, dialect: ConfigurableDialect) -> None:
    """filter(User.age.between(...)) 应在 SQL 中包含 BETWEEN。"""
    dialect.fetch_rows = [
        {"id": 1, "name": "Alice", "email": "alice@e.com", "age": 30},
    ]

    users = await session.query(User).filter(User.age.between(20, 40)).all()

    assert len(users) == 1
    assert "BETWEEN" in dialect.last_sql
    assert 20 in dialect.last_params
    assert 40 in dialect.last_params


# ---------------------------------------------------------------------------
# 原生 SQL 执行
# ---------------------------------------------------------------------------


@pytest.fixture
def raw_session() -> Session:
    return Session(ConfigurableDialect())


async def test_execute_raw_delegates_to_dialect(raw_session: Session) -> None:
    """execute_raw 应将 SQL 和参数透传给 dialect.execute。"""
    raw_session.dialect.execute_result = 1
    result = await raw_session.execute_raw("DELETE FROM users WHERE id = %s", [1])
    assert result == 1


async def test_fetch_raw_delegates_to_dialect(raw_session: Session) -> None:
    """fetch_raw 应返回 dialect.fetch 的结果。"""
    raw_session.dialect.fetch_rows = [{"id": 1, "name": "Alice"}]
    rows = await raw_session.fetch_raw("SELECT * FROM users")
    assert len(rows) == 1
    assert rows[0]["name"] == "Alice"


async def test_fetch_one_raw_delegates_to_dialect(raw_session: Session) -> None:
    """fetch_one_raw 应返回单行或 None。"""
    raw_session.dialect.fetchrow_result = {"id": 1}
    row = await raw_session.fetch_one_raw("SELECT * FROM users WHERE id = 1")
    assert row == {"id": 1}

    raw_session.dialect.fetchrow_result = None
    row = await raw_session.fetch_one_raw("SELECT * FROM users WHERE id = 999")
    assert row is None


# ---------------------------------------------------------------------------
# 健康检查 (ping)
# ---------------------------------------------------------------------------


async def test_ping_succeeds(raw_session: Session) -> None:
    """ping 应执行 SELECT 1 并返回 True。"""
    raw_session.dialect.fetchrow_result = {"ok": 1}
    ok = await raw_session.dialect.ping()
    assert ok is True


async def test_ping_fails_on_exception(raw_session: Session) -> None:
    """ping 异常时应返回 False 而非抛异常。"""

    class FailingDialect(ConfigurableDialect):
        async def fetchrow(self, sql: str, params: list[Any]) -> dict[str, Any] | None:
            raise RuntimeError("connection lost")

    dialect = FailingDialect()
    ok = await dialect.ping()
    assert ok is False
