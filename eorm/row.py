from __future__ import annotations

from typing import Any


class RowSnapshot:
    """JOIN 结果中一张表的字段快照，支持 ``snapshot.field`` 访问。"""

    __slots__ = ("_fields",)

    def __init__(self, fields: dict[str, Any]) -> None:
        object.__setattr__(self, "_fields", fields)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._fields[name]
        except KeyError:
            raise AttributeError(f"Field {name!r} not found") from None

    def __repr__(self) -> str:
        return f"RowSnapshot({self._fields})"


class Row:
    """多表 JOIN 查询的结果行。

    字段按表名分组，支持两种访问方式：

    * ``row.users.name``  —— 属性访问
    * ``row["users.name"]`` —— 字符串 key

    可通过 ``dict(row)`` 转为扁平字典。
    """

    __slots__ = ("_raw", "_tables")

    def __init__(self, raw: dict[str, Any], table_names: list[str]) -> None:
        object.__setattr__(self, "_raw", raw)
        tables: dict[str, dict[str, Any]] = {t: {} for t in table_names}
        # 预建 prefix → table 映射，避免内层循环每次做 f-string 拼接
        prefix_map = {f"{t}_": t for t in table_names}
        for key, value in raw.items():
            for prefix, table in prefix_map.items():
                if key.startswith(prefix):
                    tables[table][key[len(prefix) :]] = value
                    break
        object.__setattr__(self, "_tables", tables)

    def __getattr__(self, name: str) -> RowSnapshot:
        try:
            return RowSnapshot(self._tables[name])
        except KeyError:
            raise AttributeError(
                f"Table {name!r} not found. Available: {list(self._tables)}"
            ) from None

    def __getitem__(self, key: str) -> Any:
        """支持 ``row["users.id"]`` 和 ``row["users_id"]`` 两种写法。"""
        return self._raw[key.replace(".", "_")]

    def __iter__(self) -> Any:
        """支持 ``dict(row)`` 转为扁平字典。"""
        return iter(self._raw.items())

    def __repr__(self) -> str:
        return f"Row({self._raw})"
