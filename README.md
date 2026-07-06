# eorm

**Async ORM for Python 3.10+** — MySQL and PostgreSQL, powered by Pydantic v2.

```python
from eorm import Field, Model, connect_mysql

class User(Model):
    class Meta:
        table = "users"

    id: int = Field(primary_key=True, auto_increment=True)
    name: str = Field(max_length=100)
    age: int | None = None

async with await connect_mysql(...) as session:
    # 增
    user = User(name="Alice", age=30)
    await session.save(user)        # id 自动回填

    # 查
    alice = await session.query(User).filter(User.name == "Alice").one()
    adults = await session.query(User).filter(User.age >= 18).order_by(User.age.desc()).all()

    # 改
    alice.age = 31
    await session.save(alice)

    # 批量更新（无需先查询）
    await session.query(User).filter(User.age < 18).update(age=0)

    # 批量删除
    await session.query(User).filter(User.age == 0).delete()
```

## 特性

- **Async-first** — 所有 I/O 操作均为 `async`/`await`，基于 `asyncmy`（MySQL）和 `asyncpg`（PostgreSQL）
- **Pydantic 原生** — 模型即 Pydantic `BaseModel`，享受类型校验、序列化等所有 Pydantic 功能
- **表达式 API** — Python 运算符 `==` `!=` `>` `<` `>=` `<=` 直接生成参数化 SQL，防止注入
- **DDL 自动同步** — `Model.sync_table()` 自动建表、新增列、修改列（只增改不删）
- **JSON 原生支持** — `dict`/`list` 字段自动映射 JSON/JSONB，`json_path()` 支持路径查询
- **多表 JOIN** — 显式 ON 条件关联，返回 `Row` 对象支持属性/字典双模式访问
- **连接池复用** — Engine/连接池共享，Session 按需创建

## 安装

```bash
uv add eorm
# 或
pip install eorm
```

根据数据库选择驱动：

| 数据库 | 驱动 | 说明 |
|---|---|---|
| MySQL | `asyncmy` | 连接池 + 事务 |
| PostgreSQL | `asyncpg` | 连接池 + 事务，INSERT 自动 RETURNING |

## 快速开始

### 1. 定义模型

```python
import datetime
from eorm import Field, Model

class Article(Model):
    class Meta:
        table = "articles"

    id: int = Field(primary_key=True, auto_increment=True)
    title: str = Field(max_length=200)
    body: str | None = None
    tags: list = Field(default_factory=list)    # → JSON/JSONB
    view_count: int = Field(default=0)
    created_at: datetime.datetime | None = None
```

### 2. 连接数据库

```python
from eorm import Engine, connect_mysql, connect_postgresql, create_mysql_engine, create_postgresql_engine

# 方式 A：直接创建 Session
session = await connect_mysql(host="localhost", port=3306, user="root", password="", database="test")

# 方式 B：创建 Engine，按需产生 Session（连接池复用）
engine = await create_postgresql_engine(host="localhost", port=5432, user="postgres", password="", database="test")
session = engine.session()
```

### 3. 建表

```python
await Article.sync_table(session.dialect)
```

首次执行生成 `CREATE TABLE IF NOT EXISTS`；后续当模型新增字段时自动追加 `ALTER TABLE ADD COLUMN`；类型或 null 约束变化时自动修改列。**永不删除列或表。**

### 4. CRUD

```python
# ---- INSERT ----
article = Article(title="Hello", body="...", tags=["tech"])
await session.save(article)
print(article.id)  # auto_increment 主键自动回填

# 批量插入
articles = [Article(title=f"Post #{i}") for i in range(100)]
await session.bulk_insert(articles)

# ---- SELECT ----
# 全表
all_articles = await session.query(Article).all()

# 条件过滤
result = await session.query(Article).filter(Article.view_count >= 10).all()

# 单条（零行抛 DoesNotExist，多行抛 MultipleObjectsReturned）
item = await session.query(Article).filter(Article.id == 1).one()

# first()：有则返回，无则 None
item = await session.query(Article).filter(Article.title == "Hello").first()

# 计数 / 存在性
total = await session.query(Article).count()
has_new = await session.query(Article).filter(Article.title.like("Post%")).exists()

# 排序 + 分页
page = await session.query(Article).order_by(Article.id.desc()).limit(10).offset(20).all()

# ---- UPDATE ----
# 单条更新（查 → 改 → 存）
item = await session.query(Article).filter(Article.id == 1).one()
item.view_count += 1
await session.save(item)

# 批量更新（一条 SQL，无需先查询）
row_count = await session.query(Article).filter(Article.created_at.is_null()).update(created_at=datetime.datetime.now())

# ---- DELETE ----
# 单条删除
await session.delete(item)

# 批量删除
row_count = await session.query(Article).filter(Article.view_count == 0).delete()
```

### 5. 高级查询

```python
# LIKE / BETWEEN
users = await session.query(User).filter(User.name.like("张%")).all()
cheap = await session.query(Book).filter(Book.price.between(10, 50)).all()

# IN / IS NULL
active = await session.query(User).filter(User.role.in_(["admin", "editor"])).all()
unknown = await session.query(User).filter(User.email.is_null()).all()

# AND / OR
cond = (User.age >= 18) & (User.role == "member")      # AND
cond = (User.role == "admin") | (User.role == "staff")  # OR
result = await session.query(User).filter(cond).all()

# JSON 路径查询
# MySQL:  `tags`->>'$[0]' = 'tech'
# PG:     "tags"->>'tech'
tech_articles = await session.query(Article).filter(Article.tags.json_path("$[0]") == "tech").all()
```

### 6. 多表 JOIN

```python
from eorm.row import Row

class Author(Model):
    class Meta: table = "authors"
    id: int = Field(primary_key=True, auto_increment=True)
    name: str

class Book(Model):
    class Meta: table = "books"
    id: int = Field(primary_key=True, auto_increment=True)
    author_id: int
    title: str
    price: float

# 两表 INNER JOIN
rows = await session.query(Author).join(
    Book, on=Author.id == Book.author_id
).filter(Book.price > 20).order_by(Author.name.asc()).all()

# LEFT JOIN
rows = await session.query(Author).join(
    Book, on=Author.id == Book.author_id, type="LEFT"
).all()

# Row 对象访问
for r in rows:
    print(r.authors.name)      # 属性访问
    print(r["authors.id"])     # 字典 key
    print(dict(r))             # 转为扁平字典
```

### 7. 事务

```python
# Session 事务
async with session.transaction():
    await session.save(alice)
    await session.save(bob)
    # 抛异常自动回滚，正常结束自动提交

# Engine 事务
async with engine.transaction() as tx:
    await tx.save(alice)
```

## 模型定义参考

### Field 参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `primary_key` | `bool` | `False` | 主键 |
| `auto_increment` | `bool` | `False` | 自增（仅 int 主键） |
| `column_name` | `str \| None` | `None` | 数据库列名（默认取 Python 属性名） |
| `nullable` | `bool` | `True` | 是否允许 NULL |
| `unique` | `bool` | `False` | 唯一约束 |
| `max_length` | `int \| None` | `None` | 字符串长度（→ VARCHAR(n)） |
| `db_type` | `str \| None` | `None` | 直接指定数据库类型（跳过自动推断） |

### Python → SQL 类型映射

| Python 类型 | MySQL | PostgreSQL |
|---|---|---|
| `int` | `INT` | `INTEGER` |
| `int` + auto_increment PK | `INT AUTO_INCREMENT` | `SERIAL` |
| `str` | `VARCHAR(n)` | `VARCHAR(n)` |
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
# 安装依赖
uv sync

# 运行测试
uv run pytest

# 类型检查
uv run mypy eorm/

# 代码风格
uv run ruff check eorm/
```

## 设计原则

- **参数化查询** — 所有 SQL 使用参数绑定，永不拼接用户输入
- **方言隔离** — MySQL 和 PostgreSQL 的差异封装在 `Dialect` 子类中，业务层完全无感
- **安全 DDL** — `sync_table()` 只做加法（新增列）和修改（ALTER COLUMN / MODIFY COLUMN），不执行 DROP
- **Pydantic 优先** — 模型定义、字段校验、序列化完全复用 Pydantic 生态

## License

MIT
