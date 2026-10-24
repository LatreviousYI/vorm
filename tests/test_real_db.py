"""for_update() 行锁的真库集成测试。

仅在设置了连接环境变量时运行（凭据不进仓库）：

    MySQL:      VORM_TEST_MYSQL_HOST / _PORT / _USER / _PASSWORD / _DATABASE
    PostgreSQL: VORM_TEST_PG_HOST / _PORT / _USER / _PASSWORD / _DATABASE

覆盖场景：
- 事务外执行守卫（RuntimeError）
- 事务内 FOR UPDATE 正常执行
- SKIP LOCKED 并发认领（两个事务拿到不同行，无双重认领）
- 行锁真实持有（他人 SKIP LOCKED 查询跳过被锁行；普通读不被阻塞）
- NOWAIT 遇锁立即报错
- 事务外读不泄漏服务器端事务（P1 验证，autocommit=True）
"""
from __future__ import annotations

import os
import re
from typing import Any

import pytest

from vorm import Field, Model, col, create_mysql_engine, create_postgresql_engine
from vorm.engine import Engine

MYSQL_HOST = os.environ.get("VORM_TEST_MYSQL_HOST", "")
MYSQL_PORT = int(os.environ.get("VORM_TEST_MYSQL_PORT", "3306"))
MYSQL_USER = os.environ.get("VORM_TEST_MYSQL_USER", "")
MYSQL_PASSWORD = os.environ.get("VORM_TEST_MYSQL_PASSWORD", "")
MYSQL_DB = os.environ.get("VORM_TEST_MYSQL_DATABASE", "")

PG_HOST = os.environ.get("VORM_TEST_PG_HOST", "")
PG_PORT = int(os.environ.get("VORM_TEST_PG_PORT", "5432"))
PG_USER = os.environ.get("VORM_TEST_PG_USER", "")
PG_PASSWORD = os.environ.get("VORM_TEST_PG_PASSWORD", "")
PG_DB = os.environ.get("VORM_TEST_PG_DATABASE", "")

TABLE = "_vorm_lock_test"


class LockMessage(Model):
    class Meta:
        table = TABLE

    id: int = Field(primary_key=True, auto_increment=True)
    # max_length → VARCHAR(32)：MySQL 不允许 TEXT 列带 DEFAULT（vorm DDL 已知问题）
    status: str = Field(max_length=32, default="pending")
    worker: str | None = None


async def _prepare(engine: Engine) -> None:
    """建表并插入 3 条 pending 消息。"""
    session = engine.session()
    await session.execute_raw(f"DROP TABLE IF EXISTS {TABLE}", [])
    await LockMessage.sync_table(session.dialect)
    for _ in range(3):
        await session.save(LockMessage(status="pending"))
    await session.close()


async def _teardown(engine: Engine) -> None:
    session = engine.session()
    await session.execute_raw(f"DROP TABLE IF EXISTS {TABLE}", [])
    await session.close()
    await engine.close()


async def _server_version(engine: Engine, sql: str) -> tuple[int, int]:
    session = engine.session()
    row = await session.fetch_one_raw(sql, [])
    await session.close()
    nums = re.findall(r"\d+", str(row["v"]))
    return int(nums[0]), int(nums[1])


# ---------------------------------------------------------------------------
# 场景（两个方言共用）
# ---------------------------------------------------------------------------


async def _scenario_guardrail(engine: Engine) -> None:
    """事务外执行 for_update() 查询必须 RuntimeError。"""
    session = engine.session()
    try:
        with pytest.raises(RuntimeError, match="inside a transaction"):
            await session.query(LockMessage).for_update().all()
        with pytest.raises(RuntimeError, match="inside a transaction"):
            await (
                session.query(LockMessage)
                .filter(col(LockMessage.status) == "pending")
                .for_update(skip_locked=True)
                .first()
            )
    finally:
        await session.close()


async def _scenario_for_update_in_transaction(engine: Engine) -> None:
    """事务内 FOR UPDATE 正常执行并返回行。"""
    session = engine.session()
    try:
        async with session.transaction():
            rows = await (
                session.query(LockMessage)
                .filter(col(LockMessage.status) == "pending")
                .order_by(col(LockMessage.id))
                .for_update()
                .all()
            )
            assert [r.id for r in rows] == [1, 2, 3]
    finally:
        await session.close()


async def _scenario_concurrent_claim(engine: Engine) -> None:
    """两个事务并发 SKIP LOCKED 认领，必须拿到不同的行。"""
    a, b = engine.session(), engine.session()
    try:
        async with a.transaction():
            msg_a = await (
                a.query(LockMessage)
                .filter(col(LockMessage.status) == "pending")
                .order_by(col(LockMessage.id))
                .limit(1)
                .for_update(skip_locked=True)
                .first()
            )
            assert msg_a is not None and msg_a.id == 1
            msg_a.status = "processing"
            msg_a.worker = "a"
            await a.save(msg_a)

            async with b.transaction():
                # b 的快照里 id=1 仍是 pending（a 未提交），但行被 a 锁定
                # → SKIP LOCKED 跳过，认领 id=2
                msg_b = await (
                    b.query(LockMessage)
                    .filter(col(LockMessage.status) == "pending")
                    .order_by(col(LockMessage.id))
                    .limit(1)
                    .for_update(skip_locked=True)
                    .first()
                )
                assert msg_b is not None and msg_b.id == 2
                msg_b.status = "processing"
                msg_b.worker = "b"
                await b.save(msg_b)

        check = engine.session()
        rows = await check.query(LockMessage).order_by(col(LockMessage.id)).all()
        assert [(r.status, r.worker) for r in rows] == [
            ("processing", "a"),
            ("processing", "b"),
            ("pending", None),
        ]
        await check.close()
    finally:
        await a.close()
        await b.close()


async def _scenario_lock_actually_held(engine: Engine) -> None:
    """行锁真实持有：他人 SKIP LOCKED 查不到被锁行，普通读不被阻塞。"""
    a, b = engine.session(), engine.session()
    try:
        async with a.transaction():
            got = await a.query(LockMessage).filter(col(LockMessage.id) == 1).for_update().first()
            assert got is not None and got.id == 1

            async with b.transaction():
                # 被锁行 → SKIP LOCKED 跳过 → None
                skipped = await (
                    b.query(LockMessage)
                    .filter(col(LockMessage.id) == 1)
                    .for_update(skip_locked=True)
                    .first()
                )
                assert skipped is None
                # 普通读走 MVCC 快照，不被行锁阻塞
                plain = await b.query(LockMessage).filter(col(LockMessage.id) == 1).first()
                assert plain is not None and plain.id == 1
    finally:
        await a.close()
        await b.close()


async def _scenario_nowait(engine: Engine) -> None:
    """NOWAIT：他人对被锁行加锁立即报错而非等待。"""
    a, b = engine.session(), engine.session()
    try:
        async with a.transaction():
            await a.query(LockMessage).filter(col(LockMessage.id) == 1).for_update().first()

            async with b.transaction():
                with pytest.raises(Exception) as exc_info:
                    await (
                        b.query(LockMessage)
                        .filter(col(LockMessage.id) == 1)
                        .for_update(nowait=True)
                        .first()
                    )
                assert "lock" in str(exc_info.value).lower()
    finally:
        await a.close()
        await b.close()


async def _scenario_nested_transaction(engine: Engine) -> None:
    """嵌套事务：内层回滚只撤销内层写入，外层照常提交。"""

    class _Boom(Exception):
        pass

    session = engine.session()
    try:
        async with session.transaction():
            await session.save(LockMessage(status="outer", worker="nested"))
            with pytest.raises(_Boom):
                async with session.transaction():
                    await session.save(LockMessage(status="inner", worker="nested"))
                    raise _Boom
            await session.save(LockMessage(status="after", worker="nested"))

        rows = await (
            session.query(LockMessage)
            .filter(col(LockMessage.worker) == "nested")
            .all()
        )
        assert sorted(row.status for row in rows) == ["after", "outer"]
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------


@pytest.fixture
async def mysql_engine() -> Any:
    engine = await create_mysql_engine(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        database=MYSQL_DB,
        minsize=1,
        maxsize=2,
        autocommit=True,
    )
    await _prepare(engine)
    yield engine
    await _teardown(engine)


@pytest.mark.skipif(not MYSQL_HOST, reason="VORM_TEST_MYSQL_HOST not set")
class TestMySQL:
    async def test_guardrail_outside_transaction(self, mysql_engine: Engine) -> None:
        await _scenario_guardrail(mysql_engine)

    async def test_for_update_inside_transaction(self, mysql_engine: Engine) -> None:
        await _scenario_for_update_in_transaction(mysql_engine)

    async def test_skip_locked_concurrent_claim(self, mysql_engine: Engine) -> None:
        version = await _server_version(mysql_engine, "SELECT VERSION() AS v")
        if version < (8, 0):
            pytest.skip(f"MySQL {version} < 8.0: no SKIP LOCKED")
        await _scenario_concurrent_claim(mysql_engine)

    async def test_lock_actually_held(self, mysql_engine: Engine) -> None:
        version = await _server_version(mysql_engine, "SELECT VERSION() AS v")
        if version < (8, 0):
            pytest.skip(f"MySQL {version} < 8.0: no SKIP LOCKED")
        await _scenario_lock_actually_held(mysql_engine)

    async def test_nowait(self, mysql_engine: Engine) -> None:
        version = await _server_version(mysql_engine, "SELECT VERSION() AS v")
        if version < (8, 0):
            pytest.skip(f"MySQL {version} < 8.0: no NOWAIT")
        await _scenario_nowait(mysql_engine)

    async def test_nested_transaction_savepoint(self, mysql_engine: Engine) -> None:
        await _scenario_nested_transaction(mysql_engine)

    async def test_reads_outside_transaction_leave_no_open_trx(
        self, mysql_engine: Engine
    ) -> None:
        """P1 验证：autocommit=True 下事务外读不在服务器端留下打开的事务。"""
        session = mysql_engine.session()
        try:
            row = await session.fetch_one_raw("SELECT @@autocommit AS ac", [])
            assert row["ac"] == 1

            for _ in range(3):
                await session.query(LockMessage).all()
                await session.fetch_one_raw("SELECT 1 AS ok", [])

            rows = await session.fetch_raw(
                "SELECT COUNT(*) AS n FROM information_schema.innodb_trx "
                "WHERE trx_mysql_thread_id IN "
                "(SELECT id FROM information_schema.processlist WHERE user = %s AND db = %s)",
                [MYSQL_USER, MYSQL_DB],
            )
            assert int(rows[0]["n"]) == 0
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------


@pytest.fixture
async def pg_engine() -> Any:
    engine = await create_postgresql_engine(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        database=PG_DB,
        min_size=1,
        max_size=2,
    )
    await _prepare(engine)
    yield engine
    await _teardown(engine)


@pytest.mark.skipif(not PG_HOST, reason="VORM_TEST_PG_HOST not set")
class TestPostgreSQL:
    async def test_guardrail_outside_transaction(self, pg_engine: Engine) -> None:
        await _scenario_guardrail(pg_engine)

    async def test_for_update_inside_transaction(self, pg_engine: Engine) -> None:
        await _scenario_for_update_in_transaction(pg_engine)

    async def test_skip_locked_concurrent_claim(self, pg_engine: Engine) -> None:
        version = await _server_version(pg_engine, "SELECT current_setting('server_version') AS v")
        if version < (9, 5):
            pytest.skip(f"PostgreSQL {version} < 9.5: no SKIP LOCKED")
        await _scenario_concurrent_claim(pg_engine)

    async def test_lock_actually_held(self, pg_engine: Engine) -> None:
        version = await _server_version(pg_engine, "SELECT current_setting('server_version') AS v")
        if version < (9, 5):
            pytest.skip(f"PostgreSQL {version} < 9.5: no SKIP LOCKED")
        await _scenario_lock_actually_held(pg_engine)

    async def test_nowait(self, pg_engine: Engine) -> None:
        version = await _server_version(pg_engine, "SELECT current_setting('server_version') AS v")
        if version < (9, 5):
            pytest.skip(f"PostgreSQL {version} < 9.5: no NOWAIT")
        await _scenario_nowait(pg_engine)

    async def test_nested_transaction_savepoint(self, pg_engine: Engine) -> None:
        await _scenario_nested_transaction(pg_engine)

    async def test_reads_outside_transaction_leave_no_open_trx(self, pg_engine: Engine) -> None:
        """事务外读不在服务器端留下 idle in transaction 连接。"""
        session = pg_engine.session()
        try:
            for _ in range(3):
                await session.query(LockMessage).all()
                await session.fetch_one_raw("SELECT 1 AS ok", [])

            rows = await session.fetch_raw(
                "SELECT count(*) AS n FROM pg_stat_activity "
                "WHERE state = 'idle in transaction' AND usename = $1 AND datname = $2",
                [PG_USER, PG_DB],
            )
            assert int(rows[0]["n"]) == 0
        finally:
            await session.close()
