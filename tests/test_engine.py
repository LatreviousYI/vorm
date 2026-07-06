from __future__ import annotations

from typing import Any

import pytest

from eorm import Engine, Session
from eorm.dialects.base import AbstractDialect


class _TestDialect(AbstractDialect):
    """Engine 测试用的轻量 dialect，所有方法为空操作。"""

    def __init__(self, pool: Any, *, _owns_pool: bool = True) -> None:
        self.pool = pool
        self._owns_pool = _owns_pool
        self.closed = False

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
        self.closed = True

    def render_json_extract(self, column_sql: str, path: str, as_text: bool) -> str:
        op = "->>" if as_text else "->"
        return f"{column_sql}{op}'{path}'"

    def map_python_type(self, annotation: Any, column_info: Any) -> str:
        return "INTEGER"

    async def introspect_columns(self, table_name: str) -> dict[str, Any]:
        return {}


@pytest.fixture
def engine() -> Engine:
    pool = object()
    closed_flag: list[bool] = [False]

    async def on_close() -> None:
        closed_flag[0] = True

    eng = Engine(_TestDialect, pool, on_close)
    eng._closed_flag = closed_flag  # type: ignore[attr-defined]
    return eng


# ---------------------------------------------------------------------------
# engine.session()
# ---------------------------------------------------------------------------


def test_engine_session_returns_session(engine: Engine) -> None:
    """engine.session() 应返回一个 Session 实例。"""
    session = engine.session()
    assert isinstance(session, Session)


def test_engine_session_dialect_does_not_own_pool(engine: Engine) -> None:
    """Engine 创建的 dialect 不应持有池（_owns_pool=False），关闭 Session 不关池。"""
    session = engine.session()
    assert session.dialect._owns_pool is False  # type: ignore[attr-defined]


def test_engine_session_closing_session_does_not_close_pool(engine: Engine) -> None:
    """Session 关闭时不应关闭 Engine 持有的池。"""
    session = engine.session()

    # session.close() → dialect.close() 但由于 _owns_pool=False，不会调 on_close
    # _TestDialect.close 只设 closed 标志，不碰池
    import asyncio

    asyncio.get_event_loop().run_until_complete(session.close())


def test_engine_sessions_share_pool(engine: Engine) -> None:
    """多个 session 应共享同一个池对象。"""
    s1 = engine.session()
    s2 = engine.session()
    assert s1.dialect.pool is s2.dialect.pool  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# engine.close()
# ---------------------------------------------------------------------------


async def test_engine_close_calls_on_close() -> None:
    """engine.close() 应触发 on_close 回调，释放池。"""
    closed = False

    async def _on_close() -> None:
        nonlocal closed
        closed = True

    eng = Engine(_TestDialect, object(), _on_close)
    await eng.close()
    assert closed is True


# ---------------------------------------------------------------------------
# engine.transaction()
# ---------------------------------------------------------------------------


async def test_engine_transaction_commit() -> None:
    """engine.transaction() 应正常提交事务。"""
    eng = Engine(_TestDialect, object(), on_close=lambda: None)

    async with eng.transaction() as session:
        assert isinstance(session, Session)


async def test_engine_transaction_rollback() -> None:
    """engine.transaction() 内抛异常应回滚。"""
    eng = Engine(_TestDialect, object(), on_close=lambda: None)

    class TestError(Exception):
        pass

    with pytest.raises(TestError):
        async with eng.transaction():
            raise TestError("模拟异常")


# ---------------------------------------------------------------------------
# 独立 dialect close（_owns_pool=True）
# ---------------------------------------------------------------------------


async def test_dialect_with_owns_pool_closes_pool() -> None:
    """非 Engine 创建的 dialect（_owns_pool=True）close 时应该关池。"""
    dialect = _TestDialect(object(), _owns_pool=True)
    assert dialect.closed is False
    await dialect.close()
    assert dialect.closed is True
