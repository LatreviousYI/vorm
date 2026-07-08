from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from eorm.dialects.mysql import create_mysql_dialect
from eorm.dialects.postgresql import create_postgresql_dialect
from eorm.model import Model
from eorm.query import ModelT, QuerySet


class Session:
    def __init__(self, dialect: Any) -> None:
        self.dialect = dialect

    def query(self, model: type[ModelT]) -> QuerySet[ModelT]:
        return QuerySet(self, model)

    # -- 单条增 / 改 --------------------------------------------------------

    async def save(self, instance: Model) -> None:
        pk_name = instance.primary_key_name()
        pk_value = getattr(instance, pk_name)
        if pk_value is None:
            sql, params = self.dialect.build_insert(instance)
            result = await self.dialect.execute_insert(sql, params)
            info = type(instance).__column_info__[pk_name]
            if info.auto_increment and result is not None:
                setattr(instance, pk_name, result)
            return
        sql, params = self.dialect.build_update(instance)
        await self.dialect.execute(sql, params)

    # -- 批量插入 -----------------------------------------------------------

    async def bulk_insert(self, instances: list[Model]) -> Any:
        """将多条实例合并成一条 INSERT 语句执行。

        注意：auto_increment 主键值不会回填到每个实例上。
        返回受影响的行数。
        """
        if not instances:
            return 0
        sql, params = self.dialect.build_bulk_insert(instances)
        return await self.dialect.execute(sql, params)

    # -- 单条删除 -----------------------------------------------------------

    async def delete(self, instance: Model) -> None:
        sql, params = self.dialect.build_delete(instance)
        await self.dialect.execute(sql, params)

    # -- 原生 SQL -----------------------------------------------------------

    async def execute_raw(self, sql: str, params: list[Any] | None = None) -> Any:
        """执行任意写操作（INSERT / UPDATE / DELETE / DDL），返回受影响行数。"""
        return await self.dialect.execute(sql, params or [])

    async def fetch_raw(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """执行任意 SELECT 查询，返回行字典列表。"""
        return await self.dialect.fetch(sql, params or [])

    async def fetch_one_raw(
        self, sql: str, params: list[Any] | None = None
    ) -> dict[str, Any] | None:
        """执行任意 SELECT 查询，返回单行字典或 None。"""
        return await self.dialect.fetchrow(sql, params or [])

    # -- 事务 ---------------------------------------------------------------

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Session]:
        """事务上下文管理器。

        用法::

            async with session.transaction():
                await session.save(alice)
                await session.save(bob)
                # 抛异常自动回滚，正常结束自动提交
        """
        await self.dialect.begin()
        try:
            yield self
        except Exception:
            await self.dialect.rollback()
            raise
        else:
            await self.dialect.commit()

    # -- 关闭 ---------------------------------------------------------------

    async def close(self) -> None:
        await self.dialect.close()


# ---------------------------------------------------------------------------
# 连接工厂
# ---------------------------------------------------------------------------


async def connect_mysql(
    *,
    host: str = "localhost",
    port: int = 3306,
    user: str | None = None,
    password: str = "",
    database: str | None = None,
    **kwargs: Any,
) -> Session:
    """创建 MySQL 会话。"""
    pool_kwargs: dict[str, Any] = {"host": host, "port": port, "user": user, "password": password}
    if database is not None:
        pool_kwargs["db"] = database
    pool_kwargs.update(kwargs)
    dialect = await create_mysql_dialect(**pool_kwargs)
    return Session(dialect)


async def connect_postgresql(
    *,
    host: str = "localhost",
    port: int = 5432,
    user: str | None = None,
    password: str = "",
    database: str | None = None,
    **kwargs: Any,
) -> Session:
    """创建 PostgreSQL 会话。"""
    pool_kwargs: dict[str, Any] = {"host": host, "port": port, "user": user, "password": password}
    if database is not None:
        pool_kwargs["database"] = database
    pool_kwargs.update(kwargs)
    dialect = await create_postgresql_dialect(**pool_kwargs)
    return Session(dialect)
