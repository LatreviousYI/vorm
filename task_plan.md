# eorm — 实现方案

## 目标

构建一个异步 Python ORM，具备以下特性：
- 用 **Pydantic v2** 声明模型（字段类型、验证、序列化均复用 Pydantic）
- 支持 **MySQL**（via `aiomysql`）和 **PostgreSQL**（via `asyncpg`）两种后端
- 全异步 API（`async/await`）
- 参数化 SQL，零字符串拼接，杜绝注入
- 方言（Dialect）抽象：查询构建器与具体驱动解耦

---

## 设计决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| Pydantic 版本 | v2 | 性能更好，`model_fields` API 更稳定 |
| 参数占位符 | 方言负责转换（`%s` vs `$1`）| aiomysql 用 `%s`，asyncpg 用 `$1..$N` |
| 连接池 | 每个后端原生池（`aiomysql.create_pool` / `asyncpg.create_pool`）| 避免引入第三方抽象层 |
| 查询 API | 方法链式 | 直观，IDE 友好 |
| 事务 | `async with session.transaction()` | 符合 Python 惯例 |
| 主键 | 单列整型 PK 优先 | 覆盖 90% 场景 |
| 外键 | **不支持**，关联由应用层控制 | 便于分库分表、跨库关联、业务灵活控制 |

---

## 实现阶段

### Phase 1 — 项目脚手架 [x]
- [x] `pyproject.toml`：依赖（pydantic, aiomysql, asyncpg）
- [x] `eorm/` 包骨架
- [x] 配置 `pytest`、`ruff`、`mypy`

### Phase 2 — 模型层 [x]
- [x] `fields.py`：`Field()` 函数
- [x] `model.py`：`ModelMeta` → `__table__`、`__pk__`、`__columns__`
- [x] `expression.py`：Column 表达式树

### Phase 3 — 查询构建器 [x]
- [x] `query.py`：QuerySet 链式 API
- [x] `.all()` / `.one()` / `.first()` / `.count()` / `.exists()`

### Phase 4 — 方言层 [x]
- [x] `dialects/base.py`：AbstractDialect
- [x] `dialects/mysql.py`：MySQL 方言
- [x] `dialects/postgresql.py`：PostgreSQL 方言

### Phase 5 — Session [x]
- [x] `session.py`：Session 类
- [x] `connect_mysql()` / `connect_postgresql()`
- [x] `save()` / `delete()` / `bulk_insert()`
- [x] `transaction()` → BEGIN/COMMIT/ROLLBACK
- [x] QuerySet `.update()` / `.delete()` 批量操作

### Phase 6 — 测试 [x]
- [x] 内存单元测试：模型解析、SQL 生成、查询执行

---

# 代码审计问题修复计划

> 2026-06-29 审计发现的问题，按优先级排列。

---

## ✅ 已修复

- [x] **事务空壳** — Dialect 增加 `begin/commit/rollback`，Session.transaction 真正管理事务生命周期
- [x] **MySQL 强制自动提交** — 事务内不提交，事务外保留 auto-commit
- [x] **没有批量插入** — `session.bulk_insert()` → 单条 `INSERT ... VALUES (...), (...), ...`
- [x] **没有批量更新** — `QuerySet.update(**values)` → `UPDATE ... SET ... WHERE ...`
- [x] **没有批量删除** — `QuerySet.delete()` → `DELETE FROM ... WHERE ...`
- [x] **PG INSERT RETURNING** — `build_insert` 追加 `RETURNING` 子句，`execute_insert()` 回填自增 ID
- [x] **Engine 连接池复用** — `Engine` 持有连接池，`engine.session()` 轻量创建，多 Session 共享一池
- [x] **类型检查器友好** — 移除 `setattr` 覆盖字段，metaclass `__getattribute__` 拦截；`filter`/`order_by` 放宽为 `Any`
- [x] **LIKE / BETWEEN 表达式** — `Column.like()` 和 `Column.between()`，参数化 SQL
- [x] **去除 Pydantic 私有 API** — `ModelMetaclass = type(BaseModel)` 替代私有 import
- [x] **DDL 自动建表 & 安全同步** — `Model.sync_table(dialect)`：表不存在则 CREATE，表存在则 ALTER ADD 缺失列，已有列永不修改/删除

---

## P0 —— 本周优先（阻塞实际使用）✅ 全部完成

### ~~1. PostgreSQL INSERT 自动回填 ID~~ ✅

### ~~2. 连接池复用（Engine）~~ ✅

### ~~3. 类型检查器友好（消除 Pyright 误报）~~ ✅

---

## P1 —— 两周内（重要功能缺失）

### ~~4. 多表 JOIN（无 FK 设计）~~ ✅

**设计原则**：不引入 FK 概念。模型只描述自己所属的表，不声明跨表关系。JOIN 条件由查询时显式指定，返回通用 Row 对象而非模型实例。

**为什么不用 FK**：去掉 FK 后更灵活——分库分表无约束、跨库关联由应用控制、连表条件完全自由。

**查询 API**：

```python
# JOIN 查询，on 条件显式指定
rows = await session.query(User)
    .join(Post, on=User.id == Post.user_id)
    .filter(Post.title == "Hello")
    .order_by(User.name.asc())
    .all()

# Row 对象按表名访问各表字段
for row in rows:
    print(row.user.name)      # users 表的字段
    print(row.post.title)     # posts 表的字段
    print(row["users.id"])    # 也支持字符串 key
    print(dict(row))          # 转普通 dict
```

**约定**：单表查询返回模型实例，JOIN 后返回 Row 对象（无需预定义超集模型）。

**Row 对象设计**：

```
SQL:  SELECT users.id, users.name, posts.id, posts.title
      FROM users INNER JOIN posts ON users.id = posts.user_id

返回:  {"users_id": 1, "users_name": "Alice", "posts_id": 101, "posts_title": "Hello"}
         │                                                  │
         └─ row.user  → 快照 (id=1, name="Alice")           │
                            └─ row.post  → 快照 (id=101, title="Hello")
```

**底层改动**：

| 层 | 改动 |
|---|---|
| `QuerySet` | 新增 `.join(model, on=expr, type="INNER"|"LEFT")` → 记录 join 信息 |
| `build_select` | 有 join 时列名输出为 `表名.列名 AS 表名_列名`，FROM 追加 JOIN 子句 |
| `fetch` | 返回 `list[dict]`，key 含表名前缀 `"users.id"` |
| `Row`（新增） | 按表名分组暴露字段，支持 `.table.field` 和 `["table.field"]` 两种访问 |

**文件**：`eorm/query.py`、`eorm/dialects/base.py`、新增 `eorm/row.py`

### ~~5. 表达式扩展（LIKE / BETWEEN）~~ ✅

### ~~6. 去除 Pydantic 私有 API 依赖~~ ✅

---

## P2 —— 一个月内（质量 / 稳定）

### ~~7. DDL 自动建表~~ ✅

### 8. 异常转换层

**问题**：aiomysql / asyncpg 原生异常直接打到用户层，跨数据库无法统一处理。

**方案**：Dialect 捕获驱动异常，映射为 eorm 异常：
- `IntegrityError`（唯一键冲突、FK 约束）
- `OperationalError`（连接断）
- `ProgrammingError`（SQL 语法错）

**文件**：`eorm/exceptions.py`、`eorm/dialects/*.py`

---

## P3 —— 锦上添花

| 任务 | 说明 |
|------|------|
| `__await__` | `await session.query(User).filter(...)` 等价 `.all()` |
| 异步迭代 | `async for user in session.query(User): ...` |
| Raw SQL | `session.execute_raw(sql, params)` |
| 聚合 | `GROUP BY` / `HAVING` / `SUM` / `AVG` |
| 联合主键 | 模型支持多个 `primary_key=True` |
| 索引/约束 DSL | `Field(index=True)` → DDL 生成 `CREATE INDEX` |
| 连接健康检查 | Pool 断连自动重连 + ping |
| 连接超时配置 | `connect_mysql(connect_timeout=5, pool_size=10)` |
| Migrations | 可选：基于模型 diff 生成 ALTER TABLE |

---

## 建议执行路径

```
已完成 ──── 本轮事务+批量
              │
              ├── 1. PG RETURNING      ──┐
              ├── 2. Engine 连接池      ──┤ P0（本周）
              └── 3. 类型检查器友好     ──┘
                                        │
              ┌── 4. JOIN（无 FK）      ──┐
              ├── 5. LIKE/BETWEEN       ──┤ P1（两周）
              └── 6. 去私有API依赖      ──┘
                                        │
              ├── 7. DDL 建表           ──┐
              └── 8. 异常转换           ──┤ P2（一月）

      后续 ── P3 按需挑选
```
