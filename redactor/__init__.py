"""redactor — a privacy gateway (bidirectional alias proxy).

See docs/brief.md for the design. This package currently exposes the local
encrypted store and alias mapping table schema (story S0.3).
"""

from redactor.detect import (
    DetectedEntity,
    Detector,
    PresidioDetector,
    finance_detector,
)
from redactor.scrub import (
    PLACEHOLDER,
    ScrubHit,
    ScrubResult,
    scrub,
    scrub_text,
)
from redactor.store import (
    SCHEMA_VERSION,
    BadKeyError,
    MappingTable,
    MigrationError,
    Sealed,
    Store,
    StoreError,
    open_store,
)

__version__ = "0.0.1"

__all__ = [
    "BadKeyError",
    "DetectedEntity",
    "Detector",
    "MappingTable",
    "MigrationError",
    "PLACEHOLDER",
    "PresidioDetector",
    "Sealed",
    "SCHEMA_VERSION",
    "ScrubHit",
    "ScrubResult",
    "Store",
    "StoreError",
    "finance_detector",
    "open_store",
    "scrub",
    "scrub_text",
    "__version__",
]
