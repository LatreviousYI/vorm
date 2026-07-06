from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from eorm.session import Session

if TYPE_CHECKING:
    from eorm.dialects.mysql import MySQLDialect
    from eorm.dialects.postgresql import PostgreSQLDialect


class Engine:
    """数据库引擎：持有连接池，按需创建 Session。

    Session 共享同一个连接池，各自维护独立的事务状态。
    """

    def __init__(
        self,
        dialect_class: type[MySQLDialect | PostgreSQLDialect],
        pool: Any,
        on_close: Callable[[], Any],
    ) -> None:
        self._dialect_class = dialect_class
        self._pool = pool
        self._on_close = on_close

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


async def create_mysql_engine(
    *,
    host: str = "localhost",
    port: int = 3306,
    user: str | None = None,
    password: str = "",
    database: str | None = None,
    **kwargs: Any,
) -> Engine:
    """创建 MySQL 引擎，连接池在引擎生命周期内复用。"""
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

    return Engine(MySQLDialect, pool, _close)


async def create_postgresql_engine(
    *,
    host: str = "localhost",
    port: int = 5432,
    user: str | None = None,
    password: str = "",
    database: str | None = None,
    **kwargs: Any,
) -> Engine:
    """创建 PostgreSQL 引擎，连接池在引擎生命周期内复用。"""
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

    return Engine(PostgreSQLDialect, pool, _close)
