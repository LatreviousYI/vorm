from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from eorm.session import Session

logger = logging.getLogger("eorm")

if TYPE_CHECKING:
    from eorm.dialects.mysql import MySQLDialect
    from eorm.dialects.postgresql import PostgreSQLDialect

_PoolFactory = Callable[[], Any]  # async () -> (pool, on_close)


class Engine:
    """数据库引擎：持有连接池，按需创建 Session。

    Session 共享同一个连接池，各自维护独立的事务状态。
    支持断线重连——调用 :meth:`reconnect` 或使用 :meth:`health_monitor` 自动检测。
    """

    def __init__(
        self,
        dialect_class: type[MySQLDialect | PostgreSQLDialect],
        pool: Any,
        on_close: Callable[[], Any],
        factory: _PoolFactory | None = None,
    ) -> None:
        self._dialect_class = dialect_class
        self._pool = pool
        self._on_close = on_close
        self._factory = factory

    def session(self) -> Session:
        """基于共享连接池创建一个新 Session。"""
        dialect = self._dialect_class(self._pool, _owns_pool=False)
        return Session(dialect)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Session]:
        """便捷事务：自动创建 Session 并管理事务生命周期。"""
        session = self.session()
        async with session.transaction():
            yield session

    async def close(self) -> None:
        """关闭连接池，释放所有连接。"""
        await self._on_close()

    # ------------------------------------------------------------------
    # 健康检查 & 重连
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        """快捷健康检查。"""
        session = self.session()
        ok = await session.dialect.ping()
        if not ok:
            logger.warning("健康检查失败：数据库不可达")
        return ok

    async def reconnect(self) -> bool:
        """关闭旧连接池，创建新连接池。成功返回 True。"""
        if self._factory is None:
            logger.warning("重连失败：未设置工厂函数")
            return False
        logger.info("正在重连数据库...")
        try:
            await self._on_close()
        except Exception:
            pass
        try:
            self._pool, self._on_close = await self._factory()
            logger.info("数据库重连成功")
            return True
        except Exception as exc:
            logger.error("数据库重连失败：%s", exc)
            return False

    async def health_monitor(self, interval: float = 30) -> None:
        """后台协程：周期 ping，断连自动重连。永不退出。

        用法::

            asyncio.create_task(engine.health_monitor(interval=10))
        """
        logger.info("健康监控已启动，间隔 %ss", interval)
        while True:
            try:
                ok = await self.ping()
            except Exception as exc:
                logger.warning("Ping 异常：%s", exc)
                ok = False

            if not ok:
                await self.reconnect()

            await asyncio.sleep(interval)


# ---------------------------------------------------------------------------
# 连接工厂
# ---------------------------------------------------------------------------


async def create_mysql_engine(
    *,
    host: str = "localhost",
    port: int = 3306,
    user: str | None = None,
    password: str = "",
    database: str | None = None,
    **kwargs: Any,
) -> Engine:
    """创建 MySQL 引擎，连接池在引擎生命周期内复用。

    支持 :meth:`Engine.reconnect` 断线重连。
    """
    try:
        import asyncmy
    except ImportError:
        raise ImportError(
            "MySQL support requires the 'asyncmy' package. Install with: pip install asyncmy"
        ) from None

    from eorm.dialects.mysql import MySQLDialect

    pool_kwargs: dict[str, Any] = {"host": host, "port": port, "user": user, "password": password}
    if database is not None:
        pool_kwargs["db"] = database
    pool_kwargs.update(kwargs)
    pool = await asyncmy.create_pool(**pool_kwargs)

    async def _close() -> None:
        pool.close()
        await pool.wait_closed()

    async def _factory() -> Any:
        new_pool = await asyncmy.create_pool(**pool_kwargs)

        async def _new_close() -> None:
            new_pool.close()
            await new_pool.wait_closed()

        return new_pool, _new_close

    return Engine(MySQLDialect, pool, _close, _factory)


async def create_postgresql_engine(
    *,
    host: str = "localhost",
    port: int = 5432,
    user: str | None = None,
    password: str = "",
    database: str | None = None,
    **kwargs: Any,
) -> Engine:
    """创建 PostgreSQL 引擎，连接池在引擎生命周期内复用。

    支持 :meth:`Engine.reconnect` 断线重连。
    """
    try:
        import asyncpg
    except ImportError:
        raise ImportError(
            "PostgreSQL support requires the 'asyncpg' package. Install with: pip install asyncpg"
        ) from None

    from eorm.dialects.postgresql import PostgreSQLDialect

    pool_kwargs: dict[str, Any] = {"host": host, "port": port, "user": user, "password": password}
    if database is not None:
        pool_kwargs["database"] = database
    pool_kwargs.update(kwargs)
    pool = await asyncpg.create_pool(**pool_kwargs)

    async def _close() -> None:
        await pool.close()

    async def _factory() -> Any:
        new_pool = await asyncpg.create_pool(**pool_kwargs)

        async def _new_close() -> None:
            await new_pool.close()

        return new_pool, _new_close

    return Engine(PostgreSQLDialect, pool, _close, _factory)
