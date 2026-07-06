# progress.md — 进度日志

## Session 2026-05-13

- [x] 分析项目现状（空脚手架，Python 3.10，uv 管理）
- [x] 调研 Pydantic v2、aiomysql、asyncpg API
- [x] 输出完整设计方案（task_plan.md）
- [ ] 等待用户确认方案，开始编码

## Session 2026-05-14

- [x] Implemented core package exports: `Model`, `Field`, `Session`, `connect`.
- [x] Implemented model metadata parsing with Pydantic v2 fields.
- [x] Implemented expression columns for filters, ordering, `IN`, and NULL checks.
- [x] Implemented backend-neutral query builder and SQL generation.
- [x] Implemented MySQL and PostgreSQL dialect wrappers.
- [x] Implemented basic `Session.save`, `delete`, `query`, `close`, and transaction placeholder.
- [x] Added unit tests for model parsing and SQL builder behavior.
- [x] Verified with `ruff`, `mypy`, and `pytest`.
