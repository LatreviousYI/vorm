"""Fast JSON serialization using orjson when available, stdlib json as fallback.

orjson is ~10-20x faster for dumps and ~5-10x faster for loads than stdlib json.
It's preferred for hot paths like SQL parameter binding and row deserialization.
"""

from __future__ import annotations

import json as _stdlib_json
from typing import Any

try:
    import orjson as _orjson  # type: ignore[import-untyped]

    _HAS_ORJSON = True
except ImportError:
    _orjson = None  # type: ignore[assignment]
    _HAS_ORJSON = False

# Re-export the exception type for except clauses (both stdlib and orjson)
JSONDecodeError = (_stdlib_json.JSONDecodeError,)
if _HAS_ORJSON:
    JSONDecodeError += (_orjson.JSONDecodeError,)  # type: ignore[attr-defined]


def dumps(obj: Any) -> str:
    """Serialize obj to a JSON string.

    When orjson is available, returns str (decoded from orjson's bytes output).
    Otherwise falls back to stdlib json.dumps.
    """
    if _HAS_ORJSON:
        return _orjson.dumps(obj).decode()  # type: ignore[union-attr]
    return _stdlib_json.dumps(obj)


def loads(s: str | bytes) -> Any:
    """Deserialize a JSON string or bytes to a Python object.

    When orjson is available, uses orjson.loads (accepts both str and bytes).
    Otherwise falls back to stdlib json.loads.
    """
    if _HAS_ORJSON:
        return _orjson.loads(s)  # type: ignore[union-attr]
    return _stdlib_json.loads(s)  # type: ignore[arg-type]