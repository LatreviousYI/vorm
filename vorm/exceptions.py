from __future__ import annotations


class VormError(Exception):
    """Base exception for vorm."""


class ModelDefinitionError(VormError):
    """Raised when a model definition cannot be mapped to a table."""


class QueryError(VormError):
    """Raised when a query cannot be built or executed."""


class DoesNotExist(QueryError):
    """Raised when one() expected a row but none was returned."""


class MultipleObjectsReturned(QueryError):
    """Raised when one() expected a single row but multiple rows were returned."""
