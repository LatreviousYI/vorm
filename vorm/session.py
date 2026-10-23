from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from vorm.dialects.base import AbstractDialect
from vorm.dialects.mysql import create_mysql_dialect
from vorm.dialects.postgresql import create_postgresql_dialect
from vorm.model import Model
from vorm.query import ModelT, QuerySet


class Session:
    def __init__(self, dialect: AbstractDialect) -> None:
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

    async def execute_raw(self, sql: str, params: list[Any] | None = None) -> int:
        """执行任意写操作（INSERT / UPDATE / DELETE / DDL），返回受影响行数。

        占位符随方言：MySQL 用 ``%s``，PostgreSQL 用 ``$1``（vorm 不改写原生 SQL）。
        不返回自增主键（``lastrowid``），需要主键请用 ``save()``；读取数据请用
        ``fetch_raw()`` / ``fetch_one_raw()``——对 ``SELECT`` 的返回值没有稳定语义。
        """
        result = await self.dialect.execute(sql, params or [])
        return int(result) if result is not None else 0

    async def fetch_raw(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """执行任意 SELECT，返回 ``list[dict]``，无结果时为 ``[]``。

        每行是驱动的原始字典，key 为 SQL 中的列标签（不做模型字段映射），值为
        驱动原生类型：``JSON`` 列是 ``str``（需自行 ``json.loads``），MySQL 的
        ``BOOL`` 是 ``0``/``1`` 的 ``int``。需要模型对象请用 ``query()``，或在
        反序列化 JSON 后交给 Pydantic 校验。
        """
        return await self.dialect.fetch(sql, params or [])

    async def fetch_one_raw(
        self, sql: str, params: list[Any] | None = None
    ) -> dict[str, Any] | None:
        """执行任意 SELECT，返回首行 ``dict``，无结果时为 ``None``（格式同 ``fetch_raw()``）。"""
        return await self.dialect.fetchrow(sql, params or [])

    # -- 事务 ---------------------------------------------------------------

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Session]:
        """事务上下文管理器，支持嵌套。

        用法::

            async with session.transaction():
                await session.save(alice)
                async with session.transaction():   # 自动使用 SAVEPOINT
                    await session.save(bob)
                # 内层抛异常只回滚内层，外层继续
                # 抛异常自动回滚，正常结束自动提交

        最外层开启真实事务；在已开启的事务中再次进入时自动创建
        ``SAVEPOINT``，内层正常退出 → 释放保存点，内层抛异常 →
        仅回滚到保存点，外层已写入的数据不受影响。
        """
        if self.dialect.in_transaction:
            async with self.dialect.savepoint():
                yield self
            return

        await self.dialect.begin()
        try:
            yield self
        except BaseException:
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
