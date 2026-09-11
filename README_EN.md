# vorm

**Async ORM for Python 3.10+** — MySQL and PostgreSQL, built on Pydantic v2. Uses Pydantic models as the data layer, with additional field attributes for database column metadata, enabling DDL table structure management. This library prioritizes simplicity and does not provide foreign key, many-to-one, or many-to-many relationship features.

## Features

- **Async-first** — All I/O is `async`/`await`, powered by `asyncmy` (MySQL) and `asyncpg` (PostgreSQL)
- **Pydantic-native** — Models are `BaseModel` subclasses with full type validation, serialization, and all Pydantic features
- **Expression API** — `==` `!=` `>` `<` `like()` `between()` `in_()` `json_path()` generate parameterized SQL
- **DDL auto-sync** — `Model.sync_table()` handles table creation, column additions, modifications, and indexes. Additive only — never drops columns, tables, or indexes
- **Native JSON support** — `dict`/`list` automatically map to JSON/JSONB; defaults use database-native functions
- **Timestamp behaviors** — `timestamp_behavior="create"|"update"|"both"` controls automatic timestamp handling
- **Index management** — `Meta.indexes` for composite indexes, `Field(index=True)` for single-column. Additive only
- **Auto-reconnect** — Automatic retry with exponential backoff on connection loss
- **Multi-table JOIN** — Explicit ON conditions with `Row` objects supporting both attribute and dictionary access
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
        timestamp_behavior="create",  # Set on INSERT, preserved on UPDATE
    )
    # MySQL → DATETIME, PG → TIMESTAMP

    updated_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="both",    # Set on both INSERT and UPDATE
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
        timestamp_behavior="create",           # INSERT writes, UPDATE preserves
    )
    updated_at: datetime.datetime = Field(
        default_factory=datetime.datetime.now,
        timestamp_behavior="both",             # Refreshed on INSERT + UPDATE
    )
```

### 2. Connect to Database

```python
from vorm.engine import create_mysql_engine, create_postgresql_engine

# MySQL connection pool (recommended)
mysql_engine = await create_mysql_engine(
    host="127.0.0.1", port=3306, user="username",
    password="password", database="test", minsize=10, maxsize=20
)
session = mysql_engine.session()

# PostgreSQL connection pool (recommended)
pg_engine = await create_postgresql_engine(
    host="127.0.0.1", port=5432, user="username",
    password="password", database="test", min_size=50, max_size=100
)
session = pg_engine.session()
```

### 3. Create Tables & Indexes

```python
await Article.sync_table(session.dialect)
```

First run → `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX`. Subsequent runs → new columns, type changes, and new indexes merged into a single `ALTER TABLE` / `CREATE INDEX`. **Never drops columns, tables, or indexes.**

### 4. CRUD

```python
# INSERT
article = Article(title="Hello", tags=["tech"])
await session.save(article)              # Single insert, id auto-populated
await session.bulk_insert([...])         # Batch insert

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
await session.save(article)              # PK not None → UPDATE
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

# Health check
ok = await session.dialect.ping()  # True → connection is healthy

# Background health monitor (auto-reconnect)
import asyncio
asyncio.create_task(engine.health_monitor(interval=10))
```

### 8. Raw SQL

```python
await session.execute_raw("DELETE FROM users WHERE status = %s", ["inactive"])
rows = await session.fetch_raw("SELECT id, name FROM users WHERE age > %s", [18])
row = await session.fetch_one_raw("SELECT * FROM users WHERE id = %s", [1])
```

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

| Value | INSERT | UPDATE |
|---|---|---|
| `"create"` | Writes value | Skips, preserves original |
| `"update"` | Writes value | Recalculates current time |
| `"both"` | Writes value | Recalculates current time |

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
