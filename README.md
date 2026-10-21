# vorm

[English](./README_EN.md)

**Async ORM for Python 3.10+** — MySQL and PostgreSQL, 基于 Pydantic v2 开发, 以pydantic作为数据模型,同时增加额外字段属性作为数据库字段必要属性,从而可以操作数据库表结构.这个库以精简为主,不提供外键以及多对一,多对多等键功能

## 特性

- **Async-first** — 所有 I/O 均为 `async`/`await`，基于 `asyncmy`（MySQL）和 `asyncpg`（PostgreSQL）
- **Pydantic 原生** — 模型即 `BaseModel`，享受类型校验、序列化等全部 Pydantic 功能
- **表达式 API** — `==` `!=` `>` `<` `like()` `between()` `in_()` `json_path()` 生成参数化 SQL
- **DDL 自动同步** — `Model.sync_table()` 建表、新增列、修改列、创建索引，只增改不删
- **JSON 原生支持** — `dict`/`list` 自动映射 JSON/JSONB，默认值使用数据库原生函数
- **时间戳行为** — `timestamp_behavior="create"|"update"|"both"` 控制自动时间戳
- **联合索引** — `Meta.indexes` 声明，`Field(index=True)` 简写，只增不删
- **断连自动重连** — health_monitor 周期 ping 检测断连，自动重建连接池（指数退避重试），已创建的 Session 无缝切换到新池
- **多表 JOIN** — 显式 ON 条件，`Row` 对象支持属性/字典双模式访问
- **行锁** — `for_update()` / `for_update(skip_locked=True)` 悲观锁，事务外执行直接报错
- **原生 SQL** — `execute_raw()` / `fetch_raw()` / `fetch_one_raw()` 绕过 ORM 直接执行

## 安装

```bash
pip install vorm
```

| 数据库 | 驱动 | 说明 |
|---|---|---|
| MySQL | `asyncmy` | 连接池 + 事务 |
| PostgreSQL | `asyncpg` | 连接池 + 事务 |

## 字段示例
```python
import datetime
from vorm import Field, Model
from vorm.ddl import Index
from enum import Enum

class FruitEnum(str, Enum):
    pear = 'pear'
    banana = 'banana'
    apple = 'apple'


class BaseField(Model):
    id_base:int|None=None

class AllFieldTypes(BaseField):
    """覆盖所有 Python 类型 → SQL 类型的映射。"""

    class Meta:
        table = "all_field_types"
        indexes = [Index(fields=("short_text",),unique=True),
                Index(fields=("age","score"))
                ]

    # -- 主键 ---------------------------------------------------------

    id: int = Field(primary_key=True, auto_increment=True)
    # MySQL    → `id` INT AUTO_INCREMENT PRIMARY KEY
    # PG       → "id" SERIAL PRIMARY KEY

    # -- 字符串 -------------------------------------------------------

    short_text: str = Field(max_length=200, nullable=True,default="",comment="短字符")
    # max_length 指定 → VARCHAR(100)

    free_text: str | None = None
    # 不指定 max_length → TEXT

    # -- 数值 ---------------------------------------------------------

    age: int | None = 0
    # MySQL → INT,  PG → INTEGER

    score: float = Field(default=0)
    # MySQL → DOUBLE,  PG → DOUBLE PRECISION

    price: decimal.Decimal | None = Field(default=0.0,db_type="decimal(15,5)")
    # MySQL → DECIMAL(18,6),  PG → DECIMAL(18,6)

    # -- 布尔 ---------------------------------------------------------

    is_active: bool = Field(default=True)
    # MySQL → BOOL,  PG → BOOLEAN

    # -- 日期时间 -----------------------------------------------------

    created_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="create",  # 数据库 INSERT 时生成，UPDATE 保留原值
    )
    # MySQL → DATETIME,  PG → TIMESTAMP

    updated_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="both",  # 数据库 INSERT 时生成，UPDATE 时自动刷新
        index=True
    )

    event_date: datetime.date | None = None
    # MySQL → DATE,  PG → DATE

    # -- 二进制 -------------------------------------------------------

    payload: bytes | None = None
    # MySQL → BLOB,  PG → BYTEA

    # -- JSON ---------------------------------------------------------

    config: dict = Field(default_factory=dict)
    # MySQL → JSON,  PG → JSONB
    # DDL: MySQL DEFAULT (JSON_OBJECT()), PG DEFAULT jsonb_build_object()

    tags: list = Field(default_factory=list)
    # MySQL → JSON,  PG → JSONB
    # DDL: MySQL DEFAULT (JSON_ARRAY()), PG DEFAULT jsonb_build_array()
    new_field:list = Field(default_factory=list)
    
    enum_f:FruitEnum = Field(default=FruitEnum.banana)
```




## 快速开始

### 1. 定义模型

```python
import datetime
from vorm import Field, Model, col
from vorm.ddl import Index

class Article(Model):
    class Meta:
        table = "articles"
        indexes = [
            Index(fields=("title",)),           # 单列索引
            Index(fields=("title", "created_at")),  # 联合索引
        ]

    id: int = Field(primary_key=True, auto_increment=True)
    title: str = Field(max_length=200)
    body: str                                 # → TEXT
    tags: list = Field(default_factory=list)  # → JSON/JSONB
    view_count: int = Field(default=0)
    created_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="create",           # 数据库 INSERT 时生成，UPDATE 保留原值
    )
    updated_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="both",             # 数据库 INSERT 时生成，UPDATE 时自动刷新
    )
```

### 2. 连接数据库

```python
from vorm.engine import create_mysql_engine, create_postgresql_engine

# MySQL 连接池（推荐）
mysql_engine = await create_mysql_engine(
    host="127.0.0.1", port=3306, user="username",
    password="password", database="test", minsize=10, maxsize=20,
    autocommit=True,  # MySQL 务必显式开启，原因见下方说明
)
session = mysql_engine.session()

# PostgreSQL 连接池（推荐）
postgresql_engine = await create_postgresql_engine(
    host="127.0.0.1", port=5432, user="username",
    password="password", database="test", min_size=50, max_size=100
)
session = postgresql_engine.session()
```

应用关闭时调用 `await mysql_engine.close()` 或 `await postgresql_engine.close()` 释放连接池。

> **重要：MySQL 连接务必传 `autocommit=True`**
>
> `asyncmy` 默认 `autocommit=False`（连接建立即执行 `SET AUTOCOMMIT = 0`）。此模式下 MySQL 服务器会为**包括 `SELECT` 在内的每条语句**隐式开启事务，事务持有 `REPEATABLE READ` 快照和已读表的元数据锁（MDL），直到 `COMMIT` / `ROLLBACK` 才释放。vorm 事务外的读查询（`query()`、`fetch_raw()`、`fetch_one_raw()`、`ping()`）归还连接前不会结束事务，连接会带着服务器端未结束的事务回到连接池被复用，后果是：
>
> - **过期读**：复用该连接的查询沿用旧事务快照，读不到其他连接已提交的最新数据，偶发且难复现
> - **DDL 阻塞**：`sync_table()` 的 `ALTER TABLE` 会被未结束事务持有的 MDL 阻塞，且 MDL 队列公平排队，这张表上的所有后续查询跟着一起卡住
>
> 传 `autocommit=True` 后，事务外的语句执行完立即自动提交，上述问题不复存在。vorm 的事务（`session.transaction()`）通过显式 `BEGIN` 开启，与 `autocommit=True` 完全兼容，行为不变。

### 3. 建表 & 索引

```python
await Article.sync_table(session.dialect)
```

首次 → `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX`。后续新增列/修改类型/新增索引分别合并为单条 `ALTER TABLE` / `CREATE INDEX`。**永不删除列、表或索引。**

### 4. CRUD

```python
# INSERT
article = Article(title="Hello", tags=["tech"])
await session.save(article)          # 单条插入，id 自动回填
await session.bulk_insert([...])     # 批量插入

# SELECT
await session.query(Article).all()                             # 全表
await session.query(Article).filter(Article.view_count >= 10).all()  # 条件
await session.query(Article).filter(Article.id == 1).one()     # 精确一条
await session.query(Article).filter(Article.title.like("Post%")).first()  # 有则返无则 None
await session.query(Article).count()                            # 计数
await session.query(Article).filter(Article.id == 1).exists()  # 存在性
await session.query(Article).order_by(col(Article.id).desc()).limit(10).offset(20).all()  # 分页

# UPDATE
article.view_count += 1
await session.save(article)          # 主键不为 None → UPDATE
await session.query(Article).filter(Article.view_count == 0).update(view_count=1)  # 批量

# DELETE
await session.delete(article)
await session.query(Article).filter(Article.view_count == 0).delete()  # 批量
```

### 排序（ORDER BY）

用 `col()` 包裹类级字段再调用方法，可消除类型检查器对类级字段的误报：

```python
from vorm import col

# 单字段
await session.query(Article).order_by(col(Article.id).desc()).all()
await session.query(Article).order_by(col(Article.id).asc()).all()

# 多字段：按传入顺序依次排序
await session.query(Article).order_by(
    col(Article.view_count).desc(),  # 先按浏览量降序
    col(Article.id).asc(),           # 再按 id 升序
).all()

# 裸字段默认升序，可与 desc()/asc() 混用
await session.query(Article).order_by(Article.status, col(Article.created_at).desc()).all()
```

> 运行时 `Article.id` 经元类拦截本就是 `Column`，`Article.id.desc()` 也能跑通；但静态类型检查器把类级字段注解为值类型 `int`，会报「`int` 无 `desc` 属性」。用 `col(Article.id)` 把它重新标注为 `Column` 即可消除误报。`col()` 对 `.like()`、`.in_()`、`.between()`、`.is_null()`、`.json_path()` 等所有 Column 方法同样适用。

### 5. 高级查询

```python
# LIKE / BETWEEN
await session.query(User).filter(col(User.name).like("张%")).all()
await session.query(Book).filter(col(Book.price).between(10, 50)).all()

# IN / IS NULL
await session.query(User).filter(col(User.role).in_(["admin", "editor"])).all()
await session.query(User).filter(col(User.email).is_null()).all()

# AND / OR
cond = (User.age >= 18) & (User.role == "member")       # AND
cond = (User.role == "admin") | (User.role == "staff")   # OR

# JSON 路径查询（MySQL: column->>'$.path' / PG: column->'key'->>'sub'）
await session.query(Article).filter(col(Article.tags).json_path("$[0]") == "tech").all()
await session.query(Product).filter(col(Product.data).json_path("$.price") > 100).all()
```

### 6. 多表 JOIN

```python
# 两表 INNER JOIN
rows = await session.query(Author).join(
    Book, on=Author.id == Book.author_id
).filter(Book.price > 20).order_by(col(Author.name).asc()).all()

# LEFT JOIN
rows = await session.query(Author).join(
    Book, on=Author.id == Book.author_id, type="LEFT"
).all()

# Row 访问
print(rows[0].authors.name)    # 属性
print(rows[0]["authors.id"])   # 字典 key
print(dict(rows[0]))           # 扁平字典

# select() 可用于查询指定字段,alias()可用于别名
d = await mysql_session.query(AllFieldTypes)\
    .join(JoinTable,on=AllFieldTypes.id==JoinTable.all_id,type="left")\
    .join(JoinTableSecond,on=JoinTable.id==JoinTableSecond.join_all_id,type="left")\
    .select(AllFieldTypes.id,AllFieldTypes.short_text,JoinTable.short_text.alias("j_s_t"),JoinTableSecond.short_text.alias("j_ss_t")).all()

class ReturnJoinTableSecond(Model):
    id: int|None

    short_text: str|None

    j_s_t: str|None
    
    j_ss_t:str|None


# 后续可以使用serialization提供的功能反序列化到pydantic模型中
from vorm.query import serialization
data = serialization(ReturnJoinTableSecond,d)

```

### 7. 事务 & 健康检查

```python
# Session 事务
async with session.transaction():
    await session.save(alice)
    await session.save(bob)
    # 异常自动回滚，正常自动提交

# 健康检查
ok = await session.dialect.ping()  # True → 连接正常
```

#### 行锁（FOR UPDATE）

`for_update()` 对查询读取的行加排他行锁（`SELECT ... FOR UPDATE`），锁持有到事务提交/回滚，用于"读到 → 写回"之间防止并发修改。**必须在事务内执行**——事务外执行直接 `RuntimeError`（autocommit 下锁瞬间释放等于没锁，非 autocommit 下锁泄漏到回池连接上无限期持有）。

```python
# 读-改-写：防止丢失更新
async with session.transaction():
    row = await (
        session.query(Account)
        .filter(col(Account.id) == 1)
        .for_update()
        .first()
    )
    row.balance -= 100
    await session.save(row)
    # 提交/回滚时锁释放

# 任务队列：SKIP LOCKED 并发认领，各 worker 互不等待
async with session.transaction():
    msg = await (
        session.query(Message)
        .filter(Message.status == "pending")
        .order_by(col(Message.id))
        .limit(1)
        .for_update(skip_locked=True)   # → FOR UPDATE SKIP LOCKED
        .first()
    )
    if msg is not None:
        msg.status = "processing"
        await session.save(msg)
    # 提交后行锁释放，status='processing' 对所有人可见
```

- `for_update(skip_locked=True)`：跳过其他事务锁定的行而不等待（MySQL 8.0+ / PostgreSQL 9.5+）
- `for_update(nowait=True)`：遇到锁定的行立即报错而不等待；与 `skip_locked` 互斥
- 与 `count()` / `exists()` / `update()` / `delete()` 组合会直接 `ValueError`（锁子句会被静默丢弃或 SQL 非法）
- 事务要尽量短：锁持有时间 = 整个事务时间，别在持锁时做网络 I/O

#### 连接池重连

断连后 `reconnect()` 会关闭旧连接池，并用**创建引擎时的同一份连接参数**（host/port/user/password 等）重建连接池。建池失败自动指数退避重试（默认 3 次：0.5s → 1s → 2s），全部失败返回 `False`。

```python
import asyncio

# 方式一：后台健康监控，断连自动重连（推荐）
asyncio.create_task(engine.health_monitor(interval=10))

# 方式二：手动重连（可自定义重试次数与退避基数）
ok = await engine.reconnect()                          # retries=3, backoff=0.5
ok = await engine.reconnect(retries=5, backoff=1.0)    # 最多 5 次：1s → 2s → 4s → 8s → 16s

# 重连成功后，已创建的 Session 自动切换到新池，无需重建：
session = engine.session()
await engine.reconnect()
await session.execute_raw("SELECT 1", [])  # 走的是新池，正常工作
```

> **注意**：重连使用引擎创建时固化的连接参数，**不支持密码轮换**——若服务器侧修改了密码，重连会一直失败，只能用新凭据重建 Engine。需要每次重连时重新读取凭据（如 IAM 令牌、轮换密码），可通过底层 `Engine` 构造函数注入自定义 `factory`。

### 8. 原生 SQL

```python
await session.execute_raw("DELETE FROM users WHERE status = %s", ["inactive"])
rows = await session.fetch_raw("SELECT id, name FROM users WHERE age > %s", [18])
row = await session.fetch_one_raw("SELECT * FROM users WHERE id = %s", [1])
```
## 查看日志
```
import logging

# 看所有 SQL 语句
logging.getLogger("vorm").setLevel(logging.DEBUG)

# 只看 DDL 和重连信息
logging.getLogger("vorm").setLevel(logging.INFO)

# 配置输出格式
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
```

## 模型定义参考

### Field 参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `primary_key` | `bool` | `False` | 主键 |
| `auto_increment` | `bool` | `False` | 自增（仅 int 主键） |
| `column_name` | `str \| None` | `None` | 数据库列名，默认取 Python 属性名 |
| `nullable` | `bool` | `True` | 是否允许 NULL |
| `unique` | `bool` | `False` | 唯一约束 |
| `index` | `bool` | `False` | 单列索引（自动转为 Index） |
| `max_length` | `int \| None` | `None` | 字符串长度 → VARCHAR(n)；无 → TEXT |
| `db_type` | `str \| None` | `None` | 直接指定数据库类型 |
| `timestamp_behavior` | `"create" \| "update" \| "both" \| None` | `None` | 自动时间戳行为 |
| `default` / `default_factory` | — | — | Python 默认值（同时生成 DDL DEFAULT） |

### timestamp_behavior

带有 `timestamp_behavior` 的 `datetime.datetime` 字段由数据库完全接管：ORM 的
INSERT、实例 UPDATE 和 QuerySet UPDATE 都不会绑定这些列，即使模型声明了
`default_factory=datetime.datetime.now`。数据库写入后的值不会自动回填到当前实例；
需要重新查询该记录来获取最终时间。

| 值 | INSERT | UPDATE |
|---|---|---|
| `"create"` | 数据库 `CURRENT_TIMESTAMP` | 保留原值 |
| `"update"` | 数据库 `CURRENT_TIMESTAMP` | 数据库自动刷新 |
| `"both"` | 数据库 `CURRENT_TIMESTAMP` | 数据库自动刷新 |

MySQL 使用 `DEFAULT CURRENT_TIMESTAMP` 和 `ON UPDATE CURRENT_TIMESTAMP`；
PostgreSQL 使用 `DEFAULT CURRENT_TIMESTAMP` 和 VORM 管理的 `BEFORE UPDATE` trigger。
`sync_table()` 会为新表和已有表同步这些数据库规则。

### Meta 配置

```python
class Meta:
    table = "table_name"                          # 表名（必填）
    indexes = [                                    # 联合索引（可选）
        Index(fields=("col1", "col2")),            # 普通索引
        Index(fields=("col1",), unique=True),      # 唯一索引
        Index(fields=("col1", "col2"), name="idx_custom"),  # 自定义名称
    ]
```

索引名自动生成规则：`ix_{table}_{fields}`（普通），`unq_{table}_{fields}`（唯一）。

### Python → SQL 类型映射

| Python 类型 | MySQL | PostgreSQL |
|---|---|---|
| `int` | `INT` | `INTEGER` |
| `int` + auto_increment PK | `INT AUTO_INCREMENT` | `SERIAL` |
| `str` + max_length | `VARCHAR(n)` | `VARCHAR(n)` |
| `str` 无 max_length | `TEXT` | `TEXT` |
| `bool` | `BOOL` | `BOOLEAN` |
| `float` | `DOUBLE` | `DOUBLE PRECISION` |
| `bytes` | `BLOB` | `BYTEA` |
| `datetime.datetime` | `DATETIME` | `TIMESTAMP` |
| `datetime.date` | `DATE` | `DATE` |
| `decimal.Decimal` | `DECIMAL(18,6)` | `DECIMAL(18,6)` |
| `dict` | `JSON` | `JSONB` |
| `list` | `JSON` | `JSONB` |

## 开发

```bash
uv sync                     # 安装依赖
uv run pytest               # 运行测试（112 个）
uv run mypy vorm/           # 类型检查
uv run ruff check vorm/     # 代码风格
```

## 设计原则

- **参数化查询** — 所有 SQL 参数绑定，永不拼接用户输入
- **方言隔离** — MySQL / PG 差异封装在 Dialect 子类，业务层无感
- **安全 DDL** — 只增不改不删（ADD COLUMN + ALTER COLUMN + CREATE INDEX，不 DROP）
- **Pydantic 优先** — 模型定义、校验、序列化完全复用 Pydantic 生态
- **数据库原生优先** — JSON 默认值使用 `JSON_OBJECT()` / `jsonb_build_object()` 等原生函数，不依赖代码序列化


## License

[Apache License](./LICENSE)
