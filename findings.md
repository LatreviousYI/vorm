# findings.md — 调研记录

## Pydantic v2 关键 API

- `model_fields: dict[str, FieldInfo]` — 获取所有字段定义
- `FieldInfo.metadata` — 存储自定义约束（max_length 等）
- `FieldInfo.default` / `FieldInfo.default_factory`
- `model_validate(dict)` — 从字典构建实例（用于从 DB row 恢复）
- `model_dump()` — 序列化为字典（用于 INSERT/UPDATE）
- 子类化 `FieldInfo` 是扩展字段元数据的官方方式

## aiomysql 要点

- 占位符：`%s`（与 MySQLdb 兼容）
- 连接池：`aiomysql.create_pool(host, port, user, password, db, ...)`
- 游标：`async with pool.acquire() as conn: async with conn.cursor() as cur:`
- `cur.execute(sql, args)` — 执行
- `cur.fetchall()` → `tuple of tuples`
- `cur.lastrowid` — INSERT 后自增 ID
- 事务：`conn.begin()` / `conn.commit()` / `conn.rollback()`

## asyncpg 要点

- 占位符：`$1, $2, ...`（位置参数）
- 连接池：`asyncpg.create_pool(dsn=...)`
- `pool.fetch(sql, *args)` → `list[Record]`
- `pool.fetchrow(sql, *args)` → `Record | None`
- `pool.execute(sql, *args)`
- `pool.fetchval(sql, *args)` — 单值
- 事务：`async with pool.acquire() as conn: async with conn.transaction():`
- `Record` 支持按列名下标访问，可直接转 `dict`

## URL 解析方案

Python 标准库 `urllib.parse.urlparse` 足够：
- `mysql://user:pass@host:3306/dbname` → scheme=mysql
- `postgresql://user:pass@host:5432/dbname` → scheme=postgresql / postgres
