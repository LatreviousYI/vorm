from __future__ import annotations


class EormError(Exception):
    """Base exception for eorm."""


class ModelDefinitionError(EormError):
    """Raised when a model definition cannot be mapped to a table."""


class QueryError(EormError):
    """Raised when a query cannot be built or executed."""


class DoesNotExist(QueryError):
    """Raised when one() expected a row but none was returned."""


class MultipleObjectsReturned(QueryError):
    """Raised when one() expected a single row but multiple rows were returned."""
