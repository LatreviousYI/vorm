from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vorm import Engine, Session
from vorm.dialects.base import AbstractDialect


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

    async def introspect_indexes(self, table_name: str) -> set[str]:
        return set()

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


async def test_session_transaction_rolls_back_on_cancellation() -> None:
    """取消事务中的任务时也必须释放数据库事务。"""

    class TrackingDialect(_TestDialect):
        def __init__(self) -> None:
            super().__init__(object())
            self.rollback_called = False

        async def rollback(self) -> None:
            self.rollback_called = True

    dialect = TrackingDialect()
    session = Session(dialect)

    with pytest.raises(asyncio.CancelledError):
        async with session.transaction():
            raise asyncio.CancelledError

    assert dialect.rollback_called is True


# ---------------------------------------------------------------------------
# 嵌套事务（SAVEPOINT）
# ---------------------------------------------------------------------------


class _RecordingTxnDialect(_TestDialect):
    """记录事务相关 SQL 的 dialect，用于验证嵌套事务行为。"""

    def __init__(self) -> None:
        super().__init__(object())
        self._tx_conn: Any = None
        self.sql: list[str] = []

    async def begin(self) -> None:
        self._tx_conn = object()
        self.sql.append("BEGIN")

    async def commit(self) -> None:
        self._tx_conn = None
        self.sql.append("COMMIT")

    async def rollback(self) -> None:
        self._tx_conn = None
        self.sql.append("ROLLBACK")

    async def execute(self, sql: str, params: list[Any]) -> Any:
        self.sql.append(sql)
        return None


async def test_nested_transaction_uses_savepoint() -> None:
    """嵌套 transaction() 内层应使用 SAVEPOINT / RELEASE，而非再次 BEGIN。"""
    dialect = _RecordingTxnDialect()
    session = Session(dialect)

    async with session.transaction():
        async with session.transaction():
            pass

    assert dialect.sql == [
        "BEGIN",
        'SAVEPOINT "vorm_sp_0"',
        'RELEASE SAVEPOINT "vorm_sp_0"',
        "COMMIT",
    ]


async def test_nested_transaction_rolls_back_to_savepoint() -> None:
    """内层异常只回滚到保存点，外层事务照常提交。"""

    class InnerError(Exception):
        pass

    dialect = _RecordingTxnDialect()
    session = Session(dialect)

    async with session.transaction():
        with pytest.raises(InnerError):
            async with session.transaction():
                raise InnerError("内层失败")

    assert dialect.sql == [
        "BEGIN",
        'SAVEPOINT "vorm_sp_0"',
        'ROLLBACK TO SAVEPOINT "vorm_sp_0"',
        'RELEASE SAVEPOINT "vorm_sp_0"',
        "COMMIT",
    ]


async def test_outer_exception_rolls_back_whole_transaction() -> None:
    """内层已正常退出，外层抛异常仍需回滚整个事务（不提交）。"""

    class OuterError(Exception):
        pass

    dialect = _RecordingTxnDialect()
    session = Session(dialect)

    with pytest.raises(OuterError):
        async with session.transaction():
            async with session.transaction():
                pass
            raise OuterError("外层失败")

    assert "COMMIT" not in dialect.sql
    assert dialect.sql[-1] == "ROLLBACK"


async def test_deeply_nested_transactions_get_unique_savepoints() -> None:
    """多层嵌套每层一个独立保存点，名字不重复。"""
    dialect = _RecordingTxnDialect()
    session = Session(dialect)

    async with session.transaction():
        async with session.transaction():
            async with session.transaction():
                pass

    savepoints = [sql for sql in dialect.sql if sql.startswith("SAVEPOINT")]
    assert savepoints == ['SAVEPOINT "vorm_sp_0"', 'SAVEPOINT "vorm_sp_1"']


async def test_savepoint_outside_transaction_raises() -> None:
    """事务外创建保存点直接报错。"""
    dialect = _RecordingTxnDialect()

    with pytest.raises(RuntimeError, match="transaction in progress"):
        async with dialect.savepoint():
            pass

    assert dialect.sql == []


# ---------------------------------------------------------------------------
# 独立 dialect close（_owns_pool=True）
# ---------------------------------------------------------------------------


async def test_dialect_with_owns_pool_closes_pool() -> None:
    """非 Engine 创建的 dialect（_owns_pool=True）close 时应该关池。"""
    dialect = _TestDialect(object(), _owns_pool=True)
    assert dialect.closed is False
    await dialect.close()
    assert dialect.closed is True


# ---------------------------------------------------------------------------
# 健康检查 & 重连
# ---------------------------------------------------------------------------


class _PingDialect(AbstractDialect):
    """支持 ping 模拟的测试方言。"""

    def __init__(self, pool: Any, *, _owns_pool: bool = True, ping_ok: bool = True) -> None:
        self.pool = pool
        self._owns_pool = _owns_pool
        self.ping_ok = ping_ok
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
        return {"ok": 1} if self.ping_ok else None

    async def close(self) -> None:
        self.closed = True

    def render_json_extract(self, column_sql: str, path: str, as_text: bool) -> str:
        return column_sql

    def map_python_type(self, annotation: Any, column_info: Any) -> str:
        return "INTEGER"

    async def introspect_indexes(self, table_name: str) -> set[str]:
        return set()

    async def introspect_columns(self, table_name: str) -> dict[str, Any]:
        return {}


async def test_engine_ping_healthy() -> None:
    """Engine.ping() 应返回 True（数据库可达时）。"""
    eng = Engine(_PingDialect, object(), on_close=lambda: None)
    ok = await eng.ping()
    assert ok is True


async def test_engine_ping_unhealthy() -> None:
    """Engine.ping() 应返回 False（数据库不可达时）。"""
    eng = Engine(_PingDialect, object(), on_close=lambda: None)
    eng._dialect_class = lambda pool, **kw: _PingDialect(pool, ping_ok=False)
    ok = await eng.ping()
    assert ok is False


async def test_engine_reconnect_no_factory() -> None:
    """无 factory 时 reconnect() 返回 False。"""
    eng = Engine(_PingDialect, object(), on_close=lambda: None)
    ok = await eng.reconnect()
    assert ok is False


async def test_engine_reconnect_success() -> None:
    """有 factory 时 reconnect() 应成功更换连接池。"""
    old_pool = object()
    new_pool = object()
    closed_flag: list[bool] = [False]

    async def _factory() -> Any:
        async def _new_close() -> None:
            pass

        return new_pool, _new_close

    async def _on_close() -> None:
        closed_flag[0] = True

    eng = Engine(_PingDialect, old_pool, _on_close, _factory)
    assert eng._pool is old_pool

    ok = await eng.reconnect()
    assert ok is True
    assert closed_flag[0] is True
    assert eng._pool is new_pool


async def test_engine_health_monitor_runs_one_cycle() -> None:
    """health_monitor 至少运行一个周期不报错。"""
    eng = Engine(_PingDialect, object(), on_close=lambda: None)

    # 手动跑一个周期来验证逻辑
    import asyncio

    task = asyncio.ensure_future(eng.health_monitor(interval=0))
    await asyncio.sleep(0.05)  # 等一个周期完成
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# reconnect() 与 PoolRef
# ---------------------------------------------------------------------------


def test_pool_ref_delegates_and_swaps() -> None:
    """PoolRef 应代理属性访问并支持原地换池。"""
    from vorm.engine import PoolRef

    class _Target:
        def __init__(self, name: str) -> None:
            self.name = name

        def hello(self) -> str:
            return self.name

    old, new = _Target("old"), _Target("new")
    ref = PoolRef(old)
    assert ref.hello() == "old"
    assert ref.current() is old
    ref.swap(new)
    assert ref.hello() == "new"
    assert ref.current() is new


async def test_engine_session_survives_reconnect() -> None:
    """reconnect() 换池后，已创建的 Session 应自动使用新池。"""
    pools = [object(), object()]

    async def _noop_close() -> None:
        pass

    async def _factory() -> Any:
        return pools[1], _noop_close

    eng = Engine(_TestDialect, pools[0], _noop_close, _factory)
    session = eng.session()
    ref = session.dialect.pool

    ok = await eng.reconnect()
    assert ok is True
    assert eng._pool is pools[1]
    # 同一个 PoolRef 原地换池：旧 session 无需重建即可看到新池
    assert session.dialect.pool is ref
    assert session.dialect.pool.current() is pools[1]
    # 旧 session 仍可正常执行
    await session.execute_raw("SELECT 1", [])
    await session.close()


async def test_engine_reconnect_retries_with_backoff() -> None:
    """建池失败应按指数退避重试，最终成功返回 True。"""
    attempts = {"n": 0}

    async def _noop_close() -> None:
        pass

    async def _factory() -> Any:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ConnectionError("boom")
        return object(), _noop_close

    eng = Engine(_TestDialect, object(), _noop_close, _factory)
    ok = await eng.reconnect(retries=3, backoff=0)
    assert ok is True
    assert attempts["n"] == 3


async def test_engine_reconnect_gives_up_after_retries() -> None:
    """重试耗尽后应返回 False。"""
    attempts = {"n": 0}

    async def _noop_close() -> None:
        pass

    async def _factory() -> Any:
        attempts["n"] += 1
        raise ConnectionError("boom")

    eng = Engine(_TestDialect, object(), _noop_close, _factory)
    ok = await eng.reconnect(retries=3, backoff=0)
    assert ok is False
    assert attempts["n"] == 3


# ---------------------------------------------------------------------------
# PoolRef + asyncmy 风格 Pool（context manager acquire）正确性验证
# ---------------------------------------------------------------------------


class _RealisticPoolConn:
    """模拟 asyncmy Connection"""

    def __init__(self, conn_id: int = 0) -> None:
        self.conn_id = conn_id
        self._connected = True
        self._in_transaction = False

    @property
    def connected(self) -> bool:
        return self._connected

    def get_transaction_status(self) -> bool:
        return self._in_transaction


class _RealisticPoolAcquireCM:
    """精确模拟 asyncmy.contexts._PoolAcquireContextManager 的行为"""

    __slots__ = ("_coro", "_conn", "_pool")

    def __init__(self, coro: Any, pool: Any) -> None:
        self._coro = coro
        self._conn: Any = None
        self._pool = pool

    async def __aenter__(self) -> Any:
        self._conn = await self._coro
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        try:
            await self._pool.release(self._conn)
        finally:
            self._pool = None
            self._conn = None


class _RealisticPool:
    """模拟 asyncmy.Pool 的核心行为：
    - _free (deque), _used (set), _cond (Condition)
    - acquire() → _acquire() coroutine → _RealisticPoolAcquireCM
    - release() → _used.remove → _free.append → notify
    """

    def __init__(self, name: str = "") -> None:
        import asyncio
        from collections import deque

        self.name = name
        self._free: deque[Any] = deque()
        self._used: set[Any] = set()
        self._cond = asyncio.Condition()
        self._conn_counter = 0
        self._closing = False

    # -- 模拟预设一些空闲连接 --
    def add_free_conn(self) -> _RealisticPoolConn:
        self._conn_counter += 1
        conn = _RealisticPoolConn(self._conn_counter)
        self._free.append(conn)
        return conn

    # -- acquire() 返回 context manager（与 asyncmy 完全一致） --
    def acquire(self) -> _RealisticPoolAcquireCM:
        coro = self._acquire()
        return _RealisticPoolAcquireCM(coro, self)

    async def _acquire(self) -> _RealisticPoolConn:
        async with self._cond:
            while True:
                if self._free:
                    conn = self._free.popleft()
                    self._used.add(conn)
                    return conn
                await self._cond.wait()

    # -- release（同步方法，返回 Future，与 asyncmy 完全一致） --
    def release(self, conn: _RealisticPoolConn) -> Any:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[None] = loop.create_future()
        fut.set_result(None)

        if conn in self._used:
            self._used.remove(conn)
        if conn.connected:
            if conn.get_transaction_status():
                conn._connected = False  # 模拟 close()
            elif self._closing:
                conn._connected = False
            else:
                self._free.append(conn)
            fut = loop.create_task(self._wakeup())
        return fut

    async def _wakeup(self) -> None:
        async with self._cond:
            self._cond.notify()

    @property
    def freesize(self) -> int:
        return len(self._free)

    @property
    def usedsize(self) -> int:
        return len(self._used)

    def close(self) -> None:
        self._closing = True

    async def wait_closed(self) -> None:
        pass


async def test_poolref_non_tx_acquire_release_returns_to_correct_pool() -> None:
    """非事务路径：通过 PoolRef 获取连接 → 释放后应回到原池的 _free 中。

    这也是 MySQL dialect execute/fetch 等方法的实际执行路径。
    """
    from vorm.engine import PoolRef

    pool = _RealisticPool()
    # 预设一条空闲连接
    free_conn = pool.add_free_conn()
    assert pool.freesize == 1
    assert pool.usedsize == 0

    ref = PoolRef(pool)

    # 模拟 MySQLDialect 非事务路径的 acquire
    async with ref.acquire() as conn:
        assert conn is free_conn  # 拿到了预设的连接
        assert pool.usedsize == 1
        assert pool.freesize == 0

    # 退出 context 后：
    # - release() 检查 in_transaction → False → _free.append(conn)
    assert pool.usedsize == 0
    assert pool.freesize == 1
    assert pool._free[0] is free_conn  # 同一条连接


async def test_poolref_concurrent_acquires_dont_mix_pools() -> None:
    """两个独立池通过各自的 PoolRef 并发使用，连接不能串池。"""
    import asyncio
    from vorm.engine import PoolRef

    pool_a = _RealisticPool("A")
    pool_b = _RealisticPool("B")
    conn_a1 = pool_a.add_free_conn()
    conn_a2 = pool_a.add_free_conn()
    conn_b1 = pool_b.add_free_conn()

    ref_a = PoolRef(pool_a)
    ref_b = PoolRef(pool_b)

    async def use_pool(ref: PoolRef, expected_pool: _RealisticPool) -> _RealisticPoolConn:
        async with ref.acquire() as conn:
            await asyncio.sleep(0)  # 让出控制权，模拟并发交错
            return conn  # type: ignore[return-value]

    results = await asyncio.gather(use_pool(ref_a, pool_a), use_pool(ref_b, pool_b))

    # 两个并发获取应各自从自己的池取连接
    conns_a = {conn_a1, conn_a2}
    conns_b = {conn_b1}
    assert results[0] in conns_a
    assert results[1] in conns_b

    # 释放后各自回到自己的池
    assert pool_a.freesize == 2
    assert pool_b.freesize == 1
    assert pool_a.usedsize == 0
    assert pool_b.usedsize == 0


async def test_poolref_release_closes_conn_with_open_transaction() -> None:
    """模拟 autocommit=False 且忘记 commit：连接仍有事务时 release 应关闭连接（不放回池）。

    这是 asyncmy pool.release() 的真实行为：in_trans → conn.close()。
    """
    from vorm.engine import PoolRef

    pool = _RealisticPool()
    conn = pool.add_free_conn()
    # 模拟：执行 SQL 后事务未提交
    conn._in_transaction = True

    ref = PoolRef(pool)

    async with ref.acquire() as acquired:
        assert acquired is conn
        acquired._in_transaction = True  # 仍未提交

    # release() 应检测到 in_trans 并关闭连接，不放回 _free
    assert pool.freesize == 0
    assert pool.usedsize == 0
    assert conn.connected is False  # 被关闭了


async def test_poolref_many_rapid_acquires_maintains_pool_integrity() -> None:
    """大量快速 acquire/release 循环，验证池状态始终一致。"""
    from vorm.engine import PoolRef

    pool = _RealisticPool()
    # 预设 5 条连接
    for _ in range(5):
        pool.add_free_conn()

    ref = PoolRef(pool)

    for _ in range(100):
        async with ref.acquire() as conn:
            assert conn.connected
            assert pool.usedsize >= 1  # 至少本条在使用

    # 100 次循环后，所有连接应归还
    assert pool.freesize == 5
    assert pool.usedsize == 0


async def test_mysql_dialect_tx_releases_to_origin_pool() -> None:
    """事务连接应归还到获取它的来源池（换池后不串池）。"""
    from vorm.dialects.mysql import MySQLDialect
    from vorm.engine import PoolRef

    class _FakeConn:
        async def begin(self) -> None:
            pass

        async def commit(self) -> None:
            pass

    class _FakePool:
        def __init__(self) -> None:
            self.released: list[Any] = []

        async def acquire(self) -> Any:
            return _FakeConn()

        async def release(self, conn: Any) -> None:
            self.released.append(conn)

    old_pool = _FakePool()
    new_pool = _FakePool()
    ref = PoolRef(old_pool)
    dialect = MySQLDialect(ref, _owns_pool=False)

    await dialect.begin()
    assert dialect._tx_pool is old_pool

    ref.swap(new_pool)  # 模拟事务进行中发生重连换池
    await dialect.commit()

    # 连接归还到来源池（旧池），而不是新池
    assert len(old_pool.released) == 1
    assert len(new_pool.released) == 0
    assert dialect._tx_conn is None
    assert dialect._tx_pool is None
