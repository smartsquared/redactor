"""redactor — a privacy gateway (bidirectional alias proxy).

See docs/brief.md for the design. This package currently exposes the local
encrypted store and alias mapping table schema (story S0.3).
"""

from redactor.store import (
    BadKeyError,
    MappingTable,
    MigrationError,
    Sealed,
    SCHEMA_VERSION,
    Store,
    StoreError,
    open_store,
)

__all__ = [
    "BadKeyError",
    "MappingTable",
    "MigrationError",
    "Sealed",
    "SCHEMA_VERSION",
    "Store",
    "StoreError",
    "open_store",
]
