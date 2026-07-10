# vorm

**Async ORM for Python 3.10+** — MySQL and PostgreSQL, 基于 Pydantic v2 开发, 以pydantic作为数据模型,同时增加额外字段属性作为数据库字段必要属性,从而可以操作数据库表结构.这个库以精简为主,不提供外键以及多对一,多对多等键功能

## 特性

- **Async-first** — 所有 I/O 均为 `async`/`await`，基于 `asyncmy`（MySQL）和 `asyncpg`（PostgreSQL）
- **Pydantic 原生** — 模型即 `BaseModel`，享受类型校验、序列化等全部 Pydantic 功能
- **表达式 API** — `==` `!=` `>` `<` `like()` `between()` `in_()` `json_path()` 生成参数化 SQL
- **DDL 自动同步** — `Model.sync_table()` 建表、新增列、修改列、创建索引，只增改不删
- **JSON 原生支持** — `dict`/`list` 自动映射 JSON/JSONB，默认值使用数据库原生函数
- **时间戳行为** — `timestamp_behavior="create"|"update"|"both"` 控制自动时间戳
- **联合索引** — `Meta.indexes` 声明，`Field(index=True)` 简写，只增不删
- **断连自动重试** — 连接断开时自动重试 3 次（指数退避），非连接错误直接抛出
- **多表 JOIN** — 显式 ON 条件，`Row` 对象支持属性/字典双模式访问
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

class AllFieldTypes(Model):
    """覆盖所有 Python 类型 → SQL 类型的映射。"""

    class Meta:
        table = "all_field_types"
        indexes = [
            Index(fields=("short_text",),unique=True),
            Index(fields=("age","score")) #联合索引
        ]
    # 注意: 当数据库中的某个字段值可能为None时, 请在类型后面加上 None类型, 否则反序列化时会报错 例如: short_name :str|None
    # -- 主键 ---------------------------------------------------------
    id: int = Field(primary_key=True, auto_increment=True,db_type="bigint")
    # db_type 当设置了这个值的时候,数据库会直接使用这个值作为类型
    # MySQL    → `id` INT AUTO_INCREMENT PRIMARY KEY
    # PG       → "id" SERIAL PRIMARY KEY

    # -- 字符串 -------------------------------------------------------

    short_text: str = Field(max_length=100, nullable=True,default="")
    # max_length 指定 → VARCHAR(100)

    free_text: str | None = None
    # 不指定 max_length → TEXT

    # -- 数值 ---------------------------------------------------------

    age: int | None = 0
    # MySQL → INT,  PG → INTEGER

    score: float = Field(default=0)
    # MySQL → DOUBLE,  PG → DOUBLE PRECISION

    price: decimal.Decimal | None = Field(default=0.0,db_type="decimal(8,5)")
    # MySQL → DECIMAL(18,6),  PG → DECIMAL(18,6)

    # -- 布尔 ---------------------------------------------------------

    is_active: bool = Field(default=True)
    # MySQL → BOOL,  PG → BOOLEAN

    # -- 日期时间 -----------------------------------------------------

    created_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="create",  # INSERT 时写入，UPDATE 时跳过
    )
    # MySQL → DATETIME,  PG → TIMESTAMP

    updated_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="both",  # INSERT / UPDATE 都会刷新
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
    
```




## 快速开始

### 1. 定义模型

```python
import datetime
from vorm import Field, Model
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
        timestamp_behavior="create",           # INSERT 写入，UPDATE 保留原值
    )
    updated_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="both",             # INSERT + UPDATE 均刷新
    )
```

### 2. 连接数据库

```python
from vorm.engine import create_mysql_engine, create_postgresql_engine

# Mysql 连接池（推荐）
mysql_engine = await engine.create_mysql_engine(host="127.0.0.1",port=3306,user="username",password="password",database="test",minsize=10,maxsize=20)
session = mysql_engine.session()

# PostgreSQL 连接池（推荐）
postgresql_engine = await engine.create_postgresql_engine(host="127.0.0.1",port=3306,user="username",password="password",database="test",min_size=50,max_size=100)
session = postgresql_engine.session()
```

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
await session.query(Article).order_by(Article.id.desc()).limit(10).offset(20).all()  # 分页

# UPDATE
article.view_count += 1
await session.save(article)          # 主键不为 None → UPDATE
await session.query(Article).filter(Article.view_count == 0).update(view_count=1)  # 批量

# DELETE
await session.delete(article)
await session.query(Article).filter(Article.view_count == 0).delete()  # 批量
```

### 5. 高级查询

```python
# LIKE / BETWEEN
await session.query(User).filter(User.name.like("张%")).all()
await session.query(Book).filter(Book.price.between(10, 50)).all()

# IN / IS NULL
await session.query(User).filter(User.role.in_(["admin", "editor"])).all()
await session.query(User).filter(User.email.is_null()).all()

# AND / OR
cond = (User.age >= 18) & (User.role == "member")       # AND
cond = (User.role == "admin") | (User.role == "staff")   # OR

# JSON 路径查询（MySQL: column->>'$.path' / PG: column->'key'->>'sub'）
await session.query(Article).filter(Article.tags.json_path("$[0]") == "tech").all()
await session.query(Product).filter(Product.data.json_path("$.price") > 100).all()
```

### 6. 多表 JOIN

```python
# 两表 INNER JOIN
rows = await session.query(Author).join(
    Book, on=Author.id == Book.author_id
).filter(Book.price > 20).order_by(Author.name.asc()).all()

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


# 后续可以使用pydantic提供的功能反序列化到pydantic模型中
adapter = TypeAdapter(list[ReturnJoinTableSecond])
e: list[ReturnJoinTableSecond] = adapter.validate_python(d)

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

| 值 | INSERT | UPDATE |
|---|---|---|
| `"create"` | 写入值 | 跳过，保留原值 |
| `"update"` | 写入值 | 重新计算当前时间 |
| `"both"` | 写入值 | 重新计算当前时间 |

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
