# vorm

> **🤖 For AI Tools**: Copy the **entire content** inside the collapsible block below and paste it into any AI tool (Claude, ChatGPT, etc.). The AI will then be able to write code using vorm correctly.
>
> If your AI tool supports web fetching, you can also send it this link directly:  
> 👉 <https://raw.githubusercontent.com/LatreviousYI/vorm/master/README.md>

<details>
<summary>📋 Click to expand AI Prompt (copy everything inside)</summary>

```
You are a Python expert. Follow the vorm library's usage patterns and constraints strictly when writing code.
If you need more detailed documentation, read: https://raw.githubusercontent.com/LatreviousYI/vorm/master/README.md

# vorm — Async ORM for Python 3.10+

vorm is an async ORM built on Pydantic v2, supporting MySQL (asyncmy driver) and PostgreSQL (asyncpg driver).
No FK, many-to-one, or many-to-many relationship support — JOINs require explicit ON conditions.

## Installation

```bash
pip install vorm
```

## Model Definition

```python
import datetime
from vorm import Field, Model
from vorm.ddl import Index

class Article(Model):
    class Meta:
        table = "articles"                              # Table name (required)
        indexes = [
            Index(fields=("title",)),                    # Single-column index
            Index(fields=("title", "created_at")),       # Composite index
            Index(fields=("slug",), unique=True),         # Unique index
        ]

    id: int = Field(primary_key=True, auto_increment=True)
    title: str = Field(max_length=200)
    body: str                                           # No max_length → TEXT
    tags: list = Field(default_factory=list)             # → JSON/JSONB
    view_count: int = Field(default=0)
    created_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="create",                     # DB-generated on INSERT, preserved on UPDATE
    )
    updated_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="both",                       # DB auto-refreshed on both INSERT and UPDATE
    )
```

## Field Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `primary_key` | `bool` | `False` | Primary key |
| `auto_increment` | `bool` | `False` | Auto-increment (int PK only) |
| `column_name` | `str \| None` | `None` | DB column name, defaults to Python attribute name |
| `nullable` | `bool` | `True` | Allow NULL |
| `unique` | `bool` | `False` | Unique constraint |
| `index` | `bool` | `False` | Single-column index |
| `max_length` | `int \| None` | `None` | String length → VARCHAR(n); unset → TEXT |
| `db_type` | `str \| None` | `None` | Direct DB type override (e.g. `decimal(15,5)`) |
| `comment` | `str \| None` | `None` | Column comment |
| `timestamp_behavior` | `"create"` `"update"` `"both"` `None` | `None` | Automatic timestamp behavior |

## Python → SQL Type Mapping

| Python | MySQL | PostgreSQL |
|---|---|---|
| `int` | `INT` | `INTEGER` |
| `int` + `auto_increment` PK | `INT AUTO_INCREMENT` | `SERIAL` |
| `str` + `max_length` | `VARCHAR(n)` | `VARCHAR(n)` |
| `str` without `max_length` | `TEXT` | `TEXT` |
| `bool` | `BOOL` | `BOOLEAN` |
| `float` | `DOUBLE` | `DOUBLE PRECISION` |
| `bytes` | `BLOB` | `BYTEA` |
| `datetime.datetime` | `DATETIME` | `TIMESTAMP` |
| `datetime.date` | `DATE` | `DATE` |
| `decimal.Decimal` | `DECIMAL(18,6)` | `DECIMAL(18,6)` |
| `dict` | `JSON` | `JSONB` |
| `list` | `JSON` | `JSONB` |
| `enum.Enum` subclass | `ENUM(...)` | Custom TYPE |

## Connecting to Database

```python
from vorm.engine import create_mysql_engine, create_postgresql_engine

# MySQL — MUST pass autocommit=True!
engine = await create_mysql_engine(
    host="127.0.0.1", port=3306, user="root",
    password="pwd", database="test", minsize=10, maxsize=20,
    autocommit=True,  # REQUIRED! Prevents stale reads and DDL blocking
)
session = engine.session()

# PostgreSQL
engine = await create_postgresql_engine(
    host="127.0.0.1", port=5432, user="root",
    password="pwd", database="test", min_size=50, max_size=100,
)
session = engine.session()

# Cleanup
await engine.close()
```

## DDL Sync (create table / add columns / add indexes)

```python
await Article.sync_table(session.dialect)
# First run → CREATE TABLE IF NOT EXISTS + CREATE INDEX
# Subsequent → ALTER TABLE ADD COLUMN / MODIFY COLUMN / CREATE INDEX
# Never drops columns, tables, or indexes (additive only)
```

## CRUD

```python
# === INSERT ===
article = Article(title="Hello", tags=["tech"])
await session.insert(article)          # Always INSERT; auto-increment id auto-populated
await session.bulk_insert([...])       # Batch insert

# === save (convenience dispatcher) ===
# PK is None → INSERT, otherwise → UPDATE (NOT an upsert)
await session.save(article)

# === SELECT ===
await session.query(Article).all()                                          # All rows
await session.query(Article).filter(Article.view_count >= 10).all()         # Filtered
await session.query(Article).filter(Article.id == 1).one()                  # Exactly one (raises if not found)
await session.query(Article).filter(Article.title.like("Hello%")).first()   # First or None
await session.query(Article).count()                                        # Count
await session.query(Article).filter(Article.id == 1).exists()               # Existence check
await session.query(Article).order_by(col(Article.id).desc()).limit(10).offset(20).all()  # Pagination

# === UPDATE ===
article.view_count += 1
await session.update(article)           # Updates by PK; PK must exist
# Batch update
await session.query(Article).filter(Article.view_count == 0).update(view_count=1)

# === DELETE ===
await session.delete(article)
await session.query(Article).filter(Article.view_count == 0).delete()  # Batch
```

## Sorting

```python
from vorm import col

# Wrap class-level fields with col() to call desc()/asc() (silences type-checker false positives)
await session.query(Article).order_by(col(Article.id).desc()).all()
await session.query(Article).order_by(col(Article.id).asc()).all()

# Multi-field sorting
await session.query(Article).order_by(
    col(Article.view_count).desc(),
    col(Article.id).asc(),
).all()
```

## Advanced Queries

```python
# LIKE / BETWEEN
await session.query(User).filter(col(User.name).like("A%")).all()
await session.query(Book).filter(col(Book.price).between(10, 50)).all()

# IN / IS NULL
await session.query(User).filter(col(User.role).in_(["admin", "editor"])).all()
await session.query(User).filter(col(User.email).is_null()).all()

# AND / OR
cond = (User.age >= 18) & (User.role == "member")       # AND
cond = (User.role == "admin") | (User.role == "staff")   # OR

# JSON path queries
await session.query(Article).filter(col(Article.tags).json_path("$[0]") == "tech").all()
await session.query(Product).filter(col(Product.data).json_path("$.price") > 100).all()
```

## Multi-table JOIN

```python
# INNER JOIN
rows = await session.query(Author).join(
    Book, on=Author.id == Book.author_id
).filter(Book.price > 20).all()

# LEFT JOIN
rows = await session.query(Author).join(
    Book, on=Author.id == Book.author_id, type="LEFT"
).all()

# Row access
rows[0].authors.name     # Attribute access
rows[0]["authors.id"]    # Dict key access
dict(rows[0])            # Flat dict

# select() for specific fields + alias() for renaming
rows = await session.query(AllFieldTypes)\
    .join(JoinTable, on=AllFieldTypes.id == JoinTable.all_id, type="left")\
    .select(
        AllFieldTypes.id,
        AllFieldTypes.short_text,
        JoinTable.short_text.alias("j_s_t"),
    ).all()

# Deserialize into Pydantic models
from vorm.query import serialization
result = serialization(MyPydanticModel, rows)
```

## Transactions

```python
# Basic transaction (auto-rollback on exception, auto-commit on success)
async with session.transaction():
    await session.save(alice)
    await session.save(bob)

# Nested transaction (inner uses SAVEPOINT automatically)
async with session.transaction():
    await session.save(alice)
    try:
        async with session.transaction():
            await session.save(bob)
            raise RuntimeError("roll back bob only")
    except RuntimeError:
        pass
    # alice is still in the outer transaction
```

## Row Locking (FOR UPDATE)

```python
# Read-modify-write pattern (MUST be inside a transaction)
async with session.transaction():
    row = await session.query(Account)\
        .filter(col(Account.id) == 1)\
        .for_update()\
        .first()
    row.balance -= 100
    await session.save(row)

# SKIP LOCKED: concurrent task queue claiming
async with session.transaction():
    msg = await session.query(Message)\
        .filter(Message.status == "pending")\
        .order_by(col(Message.id))\
        .limit(1)\
        .for_update(skip_locked=True)\
        .first()
```

- for_update() with skip_locked=True / nowait=True must be inside a transaction (raises RuntimeError otherwise)
- Cannot be combined with count() / exists() / update() / delete() (raises ValueError)

## Health Check & Reconnection

```python
# Background health monitor (recommended)
asyncio.create_task(engine.health_monitor(interval=10))

# Manual ping
ok = await session.dialect.ping()

# Manual reconnect
ok = await engine.reconnect()                        # Default: 3 retries, 0.5s backoff
ok = await engine.reconnect(retries=5, backoff=1.0)  # Custom
# After successful reconnect, existing Sessions auto-switch to the new pool
```

## Raw SQL

```python
await session.execute_raw("DELETE FROM users WHERE status = %s", ["inactive"])
rows = await session.fetch_raw("SELECT id, name FROM users WHERE age > %s", [18])
row = await session.fetch_one_raw("SELECT * FROM users WHERE id = %s", [1])
```

- Placeholders: MySQL uses `%s`, PostgreSQL uses `$1, $2, ...`
- Params must be a `list` (not tuple/dict)
- fetch_raw() returns `list[dict]`, fetch_one_raw() returns `dict | None`
- execute_raw() returns affected row count (int)
- Raw SQL returns driver-native types (JSON columns are strings — use json.loads(); MySQL BOOL is int 0/1)

## Logging

```python
import logging
logging.getLogger("vorm").setLevel(logging.DEBUG)  # See all SQL
```

## Key Constraints

1. All I/O is `async/await`
2. SQL is always parameterized — never string-interpolate user input
3. DDL is additive only — never drops anything
4. MySQL connections MUST pass `autocommit=True`
5. No FK / relationship features
6. When the ORM doesn't support an operation (subqueries, window functions, UNION, complex aggregations, etc.), fall back to raw SQL (execute_raw / fetch_raw / fetch_one_raw)
```

</details>

**Async ORM for Python 3.10+** — MySQL and PostgreSQL, built on Pydantic v2. Uses Pydantic models as the data layer, with additional field attributes for database column metadata, enabling DDL table structure management. This library prioritizes simplicity and does not provide foreign key, many-to-one, or many-to-many relationship features.

## Features

- **Async-first** — All I/O is `async`/`await`, powered by `asyncmy` (MySQL) and `asyncpg` (PostgreSQL)
- **Pydantic-native** — Models are `BaseModel` subclasses with full type validation, serialization, and all Pydantic features
- **Expression API** — `==` `!=` `>` `<` `like()` `between()` `in_()` `json_path()` generate parameterized SQL
- **DDL auto-sync** — `Model.sync_table()` handles table creation, column additions, modifications, and indexes. Additive only — never drops columns, tables, or indexes
- **Native JSON support** — `dict`/`list` automatically map to JSON/JSONB; defaults use database-native functions
- **Timestamp behaviors** — `timestamp_behavior="create"|"update"|"both"` controls automatic timestamp handling
- **Index management** — `Meta.indexes` for composite indexes, `Field(index=True)` for single-column. Additive only
- **Auto-reconnect** — periodic health checks detect connection loss and rebuild the pool automatically (retried with exponential backoff); existing sessions switch to the new pool transparently
- **Multi-table JOIN** — Explicit ON conditions with `Row` objects supporting both attribute and dictionary access
- **Row locking** — `for_update()` / `for_update(skip_locked=True)` pessimistic locks; executing outside a transaction raises immediately
- **Nested transactions** — `transaction()` can be nested; inner blocks switch to `SAVEPOINT` so an inner rollback leaves the outer transaction intact
- **Raw SQL** — `execute_raw()` / `fetch_raw()` / `fetch_one_raw()` to bypass the ORM when needed
- **Enum support** — Python `enum.Enum` subclasses map to PostgreSQL custom enum types or MySQL inline `ENUM(...)`

## Installation

```bash
pip install vorm
```

| Database | Driver | Notes |
|---|---|---|
| MySQL | `asyncmy` | Connection pool + transactions |
| PostgreSQL | `asyncpg` | Connection pool + transactions |

## Field Examples

```python
import datetime
import decimal
from enum import Enum

from vorm import Field, Model
from vorm.ddl import Index

class FruitEnum(str, Enum):
    pear = 'pear'
    banana = 'banana'
    apple = 'apple'

class AllFieldTypes(Model):
    """Covers all Python type → SQL type mappings."""

    class Meta:
        table = "all_field_types"
        indexes = [
            Index(fields=("short_text",), unique=True),
            Index(fields=("age", "score")),
        ]

    # -- Primary Key ----------------------------------------------------

    id: int = Field(primary_key=True, auto_increment=True)
    # MySQL → `id` INT AUTO_INCREMENT PRIMARY KEY
    # PG    → "id" SERIAL PRIMARY KEY

    # -- String ---------------------------------------------------------

    short_text: str = Field(max_length=200, nullable=True, default="", comment="short text")
    # max_length specified → VARCHAR(200)

    free_text: str | None = None
    # No max_length → TEXT

    # -- Numeric --------------------------------------------------------

    age: int | None = 0
    # MySQL → INT, PG → INTEGER

    score: float = Field(default=0)
    # MySQL → DOUBLE, PG → DOUBLE PRECISION

    price: decimal.Decimal | None = Field(default=0.0, db_type="decimal(15,5)")
    # Custom db_type overrides automatic mapping

    # -- Boolean --------------------------------------------------------

    is_active: bool = Field(default=True)
    # MySQL → BOOL, PG → BOOLEAN

    # -- Date / Time ----------------------------------------------------

    created_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="create",  # Generated by DB on INSERT, preserved on UPDATE
    )
    # MySQL → DATETIME, PG → TIMESTAMP

    updated_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="both",    # Generated by DB on INSERT and refreshed on UPDATE
        index=True,
    )

    event_date: datetime.date | None = None
    # MySQL → DATE, PG → DATE

    # -- Binary ---------------------------------------------------------

    payload: bytes | None = None
    # MySQL → BLOB, PG → BYTEA

    # -- JSON -----------------------------------------------------------

    config: dict = Field(default_factory=dict)
    # MySQL → JSON, PG → JSONB
    # MySQL DEFAULT (JSON_OBJECT()), PG DEFAULT jsonb_build_object()

    tags: list = Field(default_factory=list)
    # MySQL → JSON, PG → JSONB
    # MySQL DEFAULT (JSON_ARRAY()), PG DEFAULT jsonb_build_array()

    # -- Enum -----------------------------------------------------------

    fruit: FruitEnum = Field(default=FruitEnum.banana)
    # PG    → CREATE TYPE fruitenum AS ENUM ('pear','banana','apple')
    # MySQL → `fruit` ENUM('pear','banana','apple')
```

## Quick Start

### 1. Define Models

```python
import datetime
from vorm import Field, Model, col
from vorm.ddl import Index

class Article(Model):
    class Meta:
        table = "articles"
        indexes = [
            Index(fields=("title",)),                  # Single-column index
            Index(fields=("title", "created_at")),     # Composite index
        ]

    id: int = Field(primary_key=True, auto_increment=True)
    title: str = Field(max_length=200)
    body: str                                  # → TEXT
    tags: list = Field(default_factory=list)   # → JSON/JSONB
    view_count: int = Field(default=0)
    created_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="create",           # Generated by DB on INSERT, preserved on UPDATE
    )
    updated_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="both",             # Generated by DB on INSERT and refreshed on UPDATE
    )
```

### 2. Connect to Database

```python
from vorm.engine import create_mysql_engine, create_postgresql_engine

# MySQL connection pool (recommended)
mysql_engine = await create_mysql_engine(
    host="127.0.0.1", port=3306, user="username",
    password="password", database="test", minsize=10, maxsize=20,
    autocommit=True,  # Required for MySQL — see note below
)
session = mysql_engine.session()

# PostgreSQL connection pool (recommended)
pg_engine = await create_postgresql_engine(
    host="127.0.0.1", port=5432, user="username",
    password="password", database="test", min_size=50, max_size=100
)
session = pg_engine.session()
```

> **Important: always pass `autocommit=True` for MySQL**
>
> `asyncmy` defaults to `autocommit=False` (`SET AUTOCOMMIT = 0` on connect). In this mode the MySQL server implicitly opens a transaction for **every statement, including `SELECT`**. That transaction holds a `REPEATABLE READ` snapshot plus metadata locks (MDL) on the tables it read until `COMMIT` / `ROLLBACK`. vorm's read queries outside explicit transactions (`query()`, `fetch_raw()`, `fetch_one_raw()`, `ping()`) return the connection to the pool without ending the transaction, so the connection is reused with an open server-side transaction. Consequences:
>
> - **Stale reads**: queries reusing that connection keep reading the old snapshot and miss data committed by other connections — intermittent and hard to reproduce
> - **DDL blocking**: `ALTER TABLE` from `sync_table()` blocks on the MDL held by the open transaction, and since the MDL queue is fair, every subsequent query on that table queues behind it
>
> With `autocommit=True`, statements outside transactions commit immediately and none of this can happen. vorm transactions (`session.transaction()`) start with an explicit `BEGIN`, fully compatible with `autocommit=True` — their behavior is unchanged.

### 3. Create Tables & Indexes

```python
await Article.sync_table(session.dialect)
```

First run → `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX`. Subsequent runs → new columns, type changes, and new indexes merged into a single `ALTER TABLE` / `CREATE INDEX`. **Never drops columns, tables, or indexes.**

### 4. CRUD

```python
# INSERT
article = Article(title="Hello", tags=["tech"])
await session.insert(article)            # Always INSERT; auto-increment id is populated
manual = Article(id=100, title="Custom ID", tags=[])
await session.insert(manual)             # A manually assigned PK is included
await session.bulk_insert([...])         # Batch insert

# save remains a convenience dispatcher: no PK -> INSERT, otherwise UPDATE; not an upsert
await session.save(article)

# SELECT
await session.query(Article).all()                                          # All rows
await session.query(Article).filter(Article.view_count >= 10).all()         # Filtered
await session.query(Article).filter(Article.id == 1).one()                  # Exactly one
await session.query(Article).filter(Article.title.like("Post%")).first()    # First or None
await session.query(Article).count()                                        # Count
await session.query(Article).filter(Article.id == 1).exists()               # Existence check
await session.query(Article).order_by(col(Article.id).desc()).limit(10).offset(20).all()  # Pagination

# UPDATE
article.view_count += 1
await session.update(article)             # Requires a PK; never inserts a missing row
# Batch update
await session.query(Article).filter(Article.view_count == 0).update(view_count=1)

# DELETE
await session.delete(article)
await session.query(Article).filter(Article.view_count == 0).delete()      # Batch
```

### Sorting (ORDER BY)

Wrap class-level fields with `col()` before calling a method to silence the type-checker's false positive:

```python
from vorm import col

# Single field
await session.query(Article).order_by(col(Article.id).desc()).all()
await session.query(Article).order_by(col(Article.id).asc()).all()

# Multiple fields: applied in the order given
await session.query(Article).order_by(
    col(Article.view_count).desc(),  # view_count descending
    col(Article.id).asc(),           # then id ascending
).all()

# Bare fields default to ascending; mix with desc()/asc()
await session.query(Article).order_by(Article.status, col(Article.created_at).desc()).all()
```

> At runtime `Article.id` is already a `Column` (via the metaclass), so `Article.id.desc()` works; but a static type checker sees the field as its value type (`int`) and reports "`int` has no `desc`". Wrapping it as `col(Article.id)` re-types it as `Column` and removes the false positive. `col()` applies to every `Column` method — `.like()`, `.in_()`, `.between()`, `.is_null()`, `.json_path()`, and so on.

### 5. Advanced Queries

```python
# LIKE / BETWEEN
await session.query(User).filter(col(User.name).like("A%")).all()
await session.query(Book).filter(col(Book.price).between(10, 50)).all()

# IN / IS NULL
await session.query(User).filter(col(User.role).in_(["admin", "editor"])).all()
await session.query(User).filter(col(User.email).is_null()).all()

# AND / OR
cond = (User.age >= 18) & (User.role == "member")       # AND
cond = (User.role == "admin") | (User.role == "staff")   # OR

# JSON path queries
await session.query(Article).filter(col(Article.tags).json_path("$[0]") == "tech").all()
await session.query(Product).filter(col(Product.data).json_path("$.price") > 100).all()
```

### 6. Multi-table JOIN

```python
# Two-table INNER JOIN
rows = await session.query(Author).join(
    Book, on=Author.id == Book.author_id
).filter(Book.price > 20).order_by(col(Author.name).asc()).all()

# LEFT JOIN
rows = await session.query(Author).join(
    Book, on=Author.id == Book.author_id, type="LEFT"
).all()

# Row access
print(rows[0].authors.name)     # Attribute access
print(rows[0]["authors.id"])    # Dict key access
print(dict(rows[0]))            # Flat dict

# select() for specific fields, alias() for renaming
rows = await session.query(AllFieldTypes)\
    .join(JoinTable, on=AllFieldTypes.id == JoinTable.all_id, type="left")\
    .join(JoinTableSecond, on=JoinTable.id == JoinTableSecond.join_all_id, type="left")\
    .select(
        AllFieldTypes.id,
        AllFieldTypes.short_text,
        JoinTable.short_text.alias("j_s_t"),
        JoinTableSecond.short_text.alias("j_ss_t"),
    ).all()

# Deserialize into Pydantic models
class ReturnJoinTableSecond(Model):
    id: int | None = None
    short_text: str | None = None
    j_s_t: str | None = None
    j_ss_t: str | None = None

from vorm.query import serialization

result = serialization(ReturnJoinTableSecond,rows)
```

### 7. Transactions & Health Check

```python
# Session transaction
async with session.transaction():
    await session.save(alice)
    await session.save(bob)
    # Auto-rollback on exception, auto-commit on success

# Nested transaction (uses SAVEPOINT automatically, only the inner part rolls back)
async with session.transaction():
    await session.save(alice)
    try:
        async with session.transaction():
            await session.save(bob)
            raise RuntimeError("roll back bob only")
    except RuntimeError:
        pass
    # alice is still in the outer transaction and can be committed

# Health check
ok = await session.dialect.ping()  # True → connection is healthy
```

#### Row Locking (FOR UPDATE)

`for_update()` takes exclusive row locks on the rows read (`SELECT ... FOR UPDATE`). Locks are held until the transaction commits or rolls back — use it to prevent lost updates between "read" and "write back". **Must run inside a transaction** — executing outside one raises `RuntimeError` immediately (under autocommit the lock is released the instant it is taken, i.e. no lock at all; without autocommit the lock leaks onto the pooled connection and is held indefinitely).

```python
# Read-modify-write: prevent lost updates
async with session.transaction():
    row = await (
        session.query(Account)
        .filter(col(Account.id) == 1)
        .for_update()
        .first()
    )
    row.balance -= 100
    await session.save(row)
    # Lock released on commit/rollback

# Task queue: concurrent claiming with SKIP LOCKED, workers never wait on each other
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
    # On commit the row lock is released and status='processing' is visible to everyone
```

- `for_update(skip_locked=True)`: skip rows locked by other transactions instead of waiting (MySQL 8.0+ / PostgreSQL 9.5+)
- `for_update(nowait=True)`: raise an error immediately on locked rows instead of waiting; mutually exclusive with `skip_locked`
- Combining with `count()` / `exists()` / `update()` / `delete()` raises `ValueError` (the lock clause would be silently dropped or the SQL is invalid)
- Keep transactions short: lock hold time = the whole transaction — never do network I/O while holding locks

#### Pool Reconnection

On connection loss, `reconnect()` closes the old pool and rebuilds it with the **same connection parameters captured at engine creation** (host/port/user/password, etc.). Failed pool creation is retried automatically with exponential backoff (3 attempts by default: 0.5s → 1s → 2s); it returns `False` once all retries are exhausted.

```python
import asyncio

# Option 1: background health monitor with auto-reconnect (recommended)
asyncio.create_task(engine.health_monitor(interval=10))

# Option 2: manual reconnect (configurable retries and backoff base)
ok = await engine.reconnect()                        # retries=3, backoff=0.5
ok = await engine.reconnect(retries=5, backoff=1.0)  # up to 5 attempts: 1s → 2s → 4s → 8s → 16s

# After a successful reconnect, existing sessions switch to the new
# pool automatically — no need to rebuild them:
session = engine.session()
await engine.reconnect()
await session.execute_raw("SELECT 1", [])  # runs on the new pool
```

> **Note**: reconnection uses the credentials fixed at engine creation — **password rotation is not supported**. If the password changes server-side, reconnect keeps failing until you build a new Engine with fresh credentials. To re-read credentials on every reconnect (e.g., IAM tokens, rotating passwords), inject a custom `factory` via the low-level `Engine` constructor.

### 8. Raw SQL

```python
await session.execute_raw("DELETE FROM users WHERE status = %s", ["inactive"])
rows = await session.fetch_raw("SELECT id, name FROM users WHERE age > %s", [18])
row = await session.fetch_one_raw("SELECT * FROM users WHERE id = %s", [1])
```

#### Return values

| Method | Returns | Notes |
|---|---|---|
| `execute_raw(sql, params=None)` | `int` | Affected row count for writes; `0` for DDL. **Does not return `lastrowid`** — use `insert()` or `save()` if you need the generated PK |
| `fetch_raw(sql, params=None)` | `list[dict[str, Any]]` | `[]` when nothing matches |
| `fetch_one_raw(sql, params=None)` | `dict[str, Any]` or `None` | `None` when nothing matches |

`params` must be a `list` (not a tuple or dict) in placeholder order; omitting it is the same as `[]`.

> `execute_raw()` is meant for writes. It will run a `SELECT`, but the return value has no stable meaning (MySQL returns the row count, PostgreSQL parses the status string) — use `fetch_raw()` / `fetch_one_raw()` to read data.

#### Rows are raw driver dicts — not models, not `Row`

```python
rows = await session.fetch_raw("SELECT id, name FROM users WHERE age > %s", [18])
# [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}]

rows[0]["name"]   # dictionary access only
rows[0].name      # ✗ AttributeError — that's a join() Row, not a raw row
```

- **Keys are the column labels from your SQL**, with no mapping to model field names (`SELECT id AS user_id` → key `user_id`)
- **Duplicate column names need aliases**: `SELECT 1 AS a, 2 AS a` becomes `{"a": 1, ".a": 2}` on MySQL (the driver renames it) and `{"a": 2}` on PostgreSQL (last one wins). Always alias in JOIN queries: `a.id AS a_id, b.id AS b_id`
- Every row is a plain `dict`, so `json.dumps`, `dict(row)` and `**row` all work

#### Placeholders are dialect-specific

vorm does not rewrite raw SQL, so placeholders must match the database:

```python
# MySQL
await session.fetch_raw("SELECT * FROM users WHERE id = %s", [1])
# PostgreSQL
await session.fetch_raw("SELECT * FROM users WHERE id = $1", [1])
```

The same statement cannot be reused across dialects; when you really need that, generate the placeholder with `session.dialect.render_placeholder(1)` (MySQL → `%s`, PostgreSQL → `$1`).

> On MySQL, a literal `%` inside the SQL must be written as `%%` (e.g. `LIKE 'abc%%'`), otherwise the driver treats it as a placeholder and raises `not enough arguments for format string`.

#### Values keep the driver's native types

`query()` decodes JSON columns into `dict`/`list` and runs Pydantic validation; **raw SQL skips all of that** and returns exactly what the driver produces:

| SQL type | MySQL (`asyncmy`) | PostgreSQL (`asyncpg`) |
|---|---|---|
| `INT` / `BIGINT` | `int` | `int` |
| `FLOAT` / `DOUBLE` | `float` | `float` |
| `DECIMAL` / `NUMERIC` | `Decimal` | `Decimal` |
| `BOOL` / `TINYINT(1)` | `int` (`0` / `1`) | `bool` |
| `DATE` | `date` | `date` |
| `DATETIME` / `TIMESTAMP` | `datetime` (naive) | `datetime` (`timestamptz` carries `tzinfo`, UTC) |
| `TIME` | `timedelta` | `time` |
| `INTERVAL` | — | `timedelta` |
| `BLOB` / `BYTEA` | `bytes` | `bytes` |
| `JSON` / `JSONB` | `str` (raw text) | `str` (raw text) |
| `ENUM` | `str` | `str` |

Two common gotchas: **JSON columns come back as strings**, so call `json.loads()` yourself; **MySQL `BOOL` is an `int` of `0`/`1`**, not `True`/`False`.

```python
import json

rows = await session.fetch_raw("SELECT id, config FROM articles", [])
articles = [{"id": r["id"], "config": json.loads(r["config"])} for r in rows]
```

#### Turning rows into models

Hand the rows to Pydantic (after `loads`-ing JSON columns if needed), or just use `query()`:

```python
from pydantic import TypeAdapter

rows = await session.fetch_raw("SELECT id, name, age FROM users", [])
users = TypeAdapter(list[User]).validate_python(rows)   # column labels must match field names

# Equivalent and simpler: go through the ORM, returns list[User]
users = await session.query(User).all()
```

#### Shared connection with transactions

Raw SQL and the ORM use the same Session/dialect, so statements issued inside a transaction take part in it and roll back together:

```python
async with session.transaction():
    await session.execute_raw("UPDATE accounts SET balance = balance - %s WHERE id = %s", [100, 1])
    await session.execute_raw("UPDATE accounts SET balance = balance + %s WHERE id = %s", [100, 2])
```

> The "must be inside a transaction" guard on `for_update()` only covers the query builder; if you hand-write `SELECT ... FOR UPDATE`, make sure it runs inside `session.transaction()` yourself.

## Logging

```python
import logging

# View all SQL statements
logging.getLogger("vorm").setLevel(logging.DEBUG)

# View only DDL and reconnection info
logging.getLogger("vorm").setLevel(logging.INFO)

# Configure output format
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
```

## Model Reference

### Field Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `primary_key` | `bool` | `False` | Primary key |
| `auto_increment` | `bool` | `False` | Auto-increment (int PK only) |
| `column_name` | `str \| None` | `None` | Database column name, defaults to Python attribute name |
| `nullable` | `bool` | `True` | Whether NULL is allowed |
| `unique` | `bool` | `False` | Unique constraint |
| `index` | `bool` | `False` | Single-column index (auto-converted to Index) |
| `max_length` | `int \| None` | `None` | String length → VARCHAR(n); unset → TEXT |
| `db_type` | `str \| None` | `None` | Direct database type override |
| `comment` | `str \| None` | `None` | Column comment (PG: COMMENT ON, MySQL: inline) |
| `timestamp_behavior` | `"create" \| "update" \| "both" \| None` | `None` | Automatic timestamp behavior |
| `default` / `default_factory` | — | — | Python default (also generates DDL DEFAULT) |

### timestamp_behavior

A `datetime.datetime` field with `timestamp_behavior` is fully managed by the
database. ORM INSERTs, instance UPDATEs, and QuerySet UPDATEs never bind these
columns, even when the model declares `default_factory=datetime.datetime.now`.
Database-generated values are not copied back to the current instance; query the
record again to obtain the final timestamps.

| Value | INSERT | UPDATE |
|---|---|---|
| `"create"` | Database `CURRENT_TIMESTAMP` | Preserve original value |
| `"update"` | Database `CURRENT_TIMESTAMP` | Refresh automatically |
| `"both"` | Database `CURRENT_TIMESTAMP` | Refresh automatically |

MySQL uses `DEFAULT CURRENT_TIMESTAMP` and `ON UPDATE CURRENT_TIMESTAMP`.
PostgreSQL uses `DEFAULT CURRENT_TIMESTAMP` and a VORM-managed `BEFORE UPDATE`
trigger. `sync_table()` synchronizes these rules for both new and existing tables.

### Meta Configuration

```python
class Meta:
    table = "table_name"                                  # Table name (required)
    indexes = [                                           # Composite indexes (optional)
        Index(fields=("col1", "col2")),                   # Regular index
        Index(fields=("col1",), unique=True),             # Unique index
        Index(fields=("col1", "col2"), name="idx_custom"), # Custom name
    ]
```

Index naming convention: `ix_{table}_{fields}` (regular), `unq_{table}_{fields}` (unique).

**Over-long index names are truncated automatically**: MySQL caps identifiers at 64 characters (error 1059 when exceeded) and PostgreSQL at 63 bytes (silently truncated). Any name (auto-generated or explicit) exceeding 63 bytes is truncated to "prefix + last 8 hex chars of the name's md5", e.g. `ix_very_long_table_very_long_field_a1b2c3d4` — the same input always yields the same result, so `sync_table()`'s existing-index comparison keeps working; different long names sharing a prefix get different hash suffixes and never collide. Prefer a short explicit `Index(name=...)` if you don't want truncation.

### Python → SQL Type Mapping

| Python Type | MySQL | PostgreSQL |
|---|---|---|
| `int` | `INT` | `INTEGER` |
| `int` + auto_increment PK | `INT AUTO_INCREMENT` | `SERIAL` |
| `str` + max_length | `VARCHAR(n)` | `VARCHAR(n)` |
| `str` without max_length | `TEXT` | `TEXT` |
| `bool` | `BOOL` | `BOOLEAN` |
| `float` | `DOUBLE` | `DOUBLE PRECISION` |
| `bytes` | `BLOB` | `BYTEA` |
| `datetime.datetime` | `DATETIME` | `TIMESTAMP` |
| `datetime.date` | `DATE` | `DATE` |
| `decimal.Decimal` | `DECIMAL(18,6)` | `DECIMAL(18,6)` |
| `dict` | `JSON` | `JSONB` |
| `list` | `JSON` | `JSONB` |
| `enum.Enum` | `ENUM(...)` | Custom type (CREATE TYPE) |

### Enum Support

Python `enum.Enum` subclasses are natively supported:

```python
import enum

class StatusEnum(str, enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"

class Article(Model):
    status: StatusEnum = Field(default=StatusEnum.DRAFT)
```

- **PostgreSQL**: Automatically generates `CREATE TYPE` before table creation. Adding new values generates `ALTER TYPE ADD VALUE`.
- **MySQL**: Automatically generates inline `ENUM('draft','published')`. Value changes trigger `MODIFY COLUMN`.
- `sync_table()` handles both dialects transparently.

## Development

```bash
uv sync                     # Install dependencies
uv run pytest               # Run tests (177 tests)
uv run mypy vorm/           # Type checking
uv run ruff check vorm/     # Linting
```

## Design Principles

- **Parameterized queries** — All SQL uses parameter binding; never interpolates user input
- **Dialect isolation** — MySQL / PostgreSQL differences are encapsulated in Dialect subclasses
- **Safe DDL** — Additive only (ADD COLUMN + ALTER COLUMN + CREATE INDEX, never DROP)
- **Pydantic-first** — Model definition, validation, and serialization fully leverage the Pydantic ecosystem
- **Database-native defaults** — JSON defaults use `JSON_OBJECT()` / `jsonb_build_object()` etc., not serialized strings

## License

[Apache License](./LICENSE)
