"""Alias-space projection: the redacted, off-machine-safe view of a statement.

This is one of the four privacy artifacts (docs/brief.md): the redacted record
that may leave the local machine. It is produced by the ingest adapter
(``redactor.ingest``) as a **separate output** from the raw transactions, which
stay in the encrypted store.

By construction a projection holds only alias-space values:

  * identifying fields (account, institution, payee) are canonical alias tokens
    (``ACCT-1``, ``INST-2``, ``PAYEE-7`` — see docs/alias-contract.md);
  * amounts and dates stay real (contract §1.1 — least-identifying, most
    conversationally necessary);
  * no raw memo / account id / institution name is carried at all.

Because it contains no mapping-table bindings and no real identifiers, this
module is free to serialize (unlike anything on the crown-jewel path): a
projection is meant to be written out and shipped into model context.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

# Schema version for the serialized projection envelope. Bump on shape changes
# so downstream consumers (S2.3 sanitized-context API) can negotiate.
PROJECTION_VERSION = 1


@dataclass(frozen=True)
class AliasRecord:
    """One transaction in alias-space. Every identifying field is a token."""

    account: str        # ACCT-n — the user's own account
    institution: str    # INST-n — the financial institution
    payee: str          # PAYEE-n — the resolved counterparty
    date: str           # ISO YYYY-MM-DD, real (not aliased)
    amount: float       # signed; negative = money out, real (not aliased)
    ttype: str = ""     # DEBIT/CREDIT when known; non-identifying
    category: str = ""  # CSV category when present; non-identifying


@dataclass(frozen=True)
class AliasSpaceProjection:
    """The full alias-space output of an ingest run."""

    records: list[AliasRecord]
    version: int = PROJECTION_VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "records": [asdict(r) for r in self.records],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def write_json(self, path: str | Path, *, indent: int | None = 2) -> Path:
        """Write the projection to *path*. Safe: alias-space only, no crown jewel."""
        p = Path(path)
        p.write_text(self.to_json(indent=indent), encoding="utf-8")
        return p
