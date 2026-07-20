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
from redactor.lens import (
    LensResult,
    Resolver,
    lens,
    resolver_from_table,
)
from redactor.outbound import (
    LeakWarning,
    OutboundRedactor,
    OutboundResult,
    Substitution,
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
    "LeakWarning",
    "LensResult",
    "MappingTable",
    "MigrationError",
    "OutboundRedactor",
    "OutboundResult",
    "PLACEHOLDER",
    "PresidioDetector",
    "Resolver",
    "Sealed",
    "Substitution",
    "SCHEMA_VERSION",
    "ScrubHit",
    "ScrubResult",
    "Store",
    "StoreError",
    "finance_detector",
    "lens",
    "open_store",
    "resolver_from_table",
    "scrub",
    "scrub_text",
    "__version__",
]
