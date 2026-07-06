# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**eorm** is an async ORM for Python 3.10+. Models are declared using Pydantic; the ORM supports MySQL via `aiomysql` and PostgreSQL via `asyncpg` as backend drivers.

## Commands

```bash
# Install dependencies (uses uv)
uv sync

# Run tests
uv run pytest

# Run a single test file
uv run pytest tests/path/to/test_file.py

# Run a single test by name
uv run pytest tests/ -k "test_name"

# Type checking
uv run mypy eorm/

# Lint
uv run ruff check eorm/
```

## Development Workflow —— 先写测试再写代码

**每次开发新功能前，必须先补测试，再实现。** 用旧测试验证新功能不准确——旧测试覆盖的是旧行为，新逻辑（SQL 生成差异、错误路径、边界条件）必须有专门的用例。

```
1. 读 task_plan.md，确认当前任务
2. 写出该功能对应的测试（此时测试会 FAIL）
3. 实现功能代码
4. 运行测试，确认 PASS
5. 标记任务完成
```

### 测试文件分工

| 文件 | 测什么 |
|---|---|
| `tests/test_model.py` | 模型元数据解析、metaclass 行为、字段访问 |
| `tests/test_query_builder.py` | SQL 生成正确性（build_select / insert / update / delete / bulk / RETURNING） |
| `tests/test_query.py` | QuerySet 执行层（.all() / .one() / .count() / .exists()） |
| `tests/test_engine.py` | Engine 连接池复用、事务、close 行为 |

### 测试模式

- SQL 生成测试用 `DummyDialect`（无需数据库）——纯函数，同步测试
- 执行层测试用 `ConfigurableDialect` / `RecordingDialect`（可配置返回值）
- Engine 测试用轻量的 `_TestDialect`

## Architecture

Current implementation：

- **`eorm/model.py`** — `Model`（继承 Pydantic `BaseModel`）+ `ModelMeta` 元类：类定义时扫描 `model_fields`，提取 `__table__`、`__columns__`、`__column_info__`、`__pk__`。使用 metaclass `__getattribute__` 拦截类级字段访问返回 Column 对象。
- **`eorm/fields.py`** — `Field()`：Pydantic Field 的包装函数，将 ORM 参数（`primary_key`、`auto_increment`、`max_length` 等）打包为 `ColumnInfo` 存入 `json_schema_extra`。
- **`eorm/expression.py`** — Column 描述符 + 表达式树（BinaryExpression、InExpression、NullExpression、CombinedExpression、OrderExpression）。
- **`eorm/dialects/base.py`** — `AbstractDialect`：定义 SQL 生成接口 + 事务 + 执行抽象（`execute`、`execute_insert`、`fetch`、`fetchrow`）。
- **`eorm/dialects/mysql.py`** — MySQL 方言（asyncmy 驱动），`%s` 占位符，backtick 引用。
- **`eorm/dialects/postgresql.py`** — PostgreSQL 方言（asyncpg 驱动），`$N` 占位符，`build_insert` 追加 `RETURNING`。
- **`eorm/query.py`** — `QuerySet`：链式 API，`.filter()` / `.order_by()` / `.limit()` / `.offset()` / `.all()` / `.one()` / `.first()` / `.count()` / `.exists()` / `.update()` / `.delete()`。
- **`eorm/session.py`** — `Session`：`save()`（INSERT/UPDATE 自动判断）、`delete()`、`bulk_insert()`、`transaction()` 上下文管理器。
- **`eorm/engine.py`** — `Engine`：持有连接池，`engine.session()` 轻量创建共享池的 Session。

### Key design constraints

- All public APIs are `async`/`await`.
- Pydantic validation runs on read (hydrating rows into model instances) and on write (validating before INSERT/UPDATE).
- SQL is always parameterised — never string-interpolated — to prevent injection.
- The `Dialect` abstraction isolates all backend-specific SQL so that `query.py` and `session.py` stay backend-agnostic.
- No FK / relationship concepts — JOIN conditions are explicit; multi-table results return Row objects.
