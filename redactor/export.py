"""Lint-attested projection export — the ``redactor export`` seam (issue #41).

The first consumer's importer (``sakuma-finance import --projection <file>``)
enforces *projection provenance*: it refuses any projection JSON that does not
carry redactor's **green lint attestation**. Nothing in redactor produced such a
file — ``redactor ingest`` builds a projection in memory and discards it,
``redactor lint`` verifies a file but stamps nothing. This module closes that
gap.

``redactor export`` does four things (the story):

  1. **Rebuild a month's alias-space projection from the store alone.** The raw
     rows (``store.raw_transactions``) plus the mapping table and payee-variant
     grouping are enough to re-derive each :class:`AliasRecord`'s identifying
     tokens offline — no statement files, no re-ingest. Account, institution, and
     payee become their canonical alias tokens; amounts and dates stay real
     (alias-contract §1.1).
  2. **Run the S0.2 detection lint over it** (:func:`redactor.lint.lint_projection`).
     The lint is the *gate*: it decides whether the projection is safe to write.
  3. **Green → write the file with a ``provenance`` attestation block** — the
     shape sakuma-finance's ``records_import.read_attestation`` reads (verdict /
     timestamp / linter version). **Red → write nothing**, hand the findings back
     for a local-render report, and let the caller exit non-zero.
  4. Keep the summary alias-space (month, row count, verdict).

Crown-jewel rule (CLAUDE.md). This module reads the mapping table only to look up
alias *tokens* (non-sensitive) for real values it already holds locally; it
serializes no binding. The one real value it may briefly place in a record is a
value the store could not alias — and that is exactly what the lint catches,
before anything is written. The provenance block is alias-space by construction
(a verdict, a timestamp, and the linter's name/version — no identifier).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from redactor.lint import LintFinding, lint_projection
from redactor.projection import AliasRecord, AliasSpaceProjection

# The linter identity stamped into the provenance block. ``LINTER_VERSION`` is
# the cross-repo attestation contract version: bump it (and the note in
# docs/sanitized-context-api.md) on any change to the provenance block shape, so
# sakuma-finance's ``records_import.read_attestation`` and this producer can never
# silently drift.
LINTER_NAME = "redactor.lint"
LINTER_VERSION = 1

# The only verdict a *written* file ever carries: a file is written only when the
# lint is green, so the attestation always reads green. A red projection is never
# written (findings go to the local render surface instead).
VERDICT_GREEN = "green"


class ExportError(Exception):
    """Export could not proceed (e.g. an unknown month). Value-free by contract."""


@dataclass(frozen=True)
class ExportResult:
    """The alias-space outcome of one month's export.

    ``verdict`` is ``"green"`` (written) or ``"red"`` (refused). ``findings`` is
    populated only for a red verdict; it echoes offending substrings, so a caller
    renders it to the local surface (stderr) only, never off-machine. ``path`` is
    the file written, or ``None`` when refused.
    """

    month: str
    row_count: int
    verdict: str
    findings: list[LintFinding]
    path: Path | None


def _variant_key(memo: str) -> str:
    """Normalize a memo to the payee-variant table's dedup key.

    Must match :func:`redactor.payees._variant_key` exactly — it is how a raw
    memo string is looked up against the grouping the registry persisted at
    ingest. Case- and whitespace-folded; nothing else stripped."""
    return " ".join(memo.split()).upper()


def _payee_token(store, raw_payee: str | None) -> str:
    """The PAYEE alias a raw memo resolved to at ingest, from the store.

    Looks the memo up in the persisted payee-variant grouping and follows any
    later merge to the canonical head, so a reviewed/merged store exports the
    entity the human confirmed. A memo the store never recorded returns the raw
    value unchanged: it is *not* silently emitted — the lint downstream refuses
    the whole projection, which is the safe failure (issue #41)."""
    if not raw_payee:
        return raw_payee or ""
    recorded = store.payee_variant(_variant_key(raw_payee))
    if recorded is None:
        return raw_payee
    token, _confidence = recorded
    return store.mapping.canonical_head(token) or token


def _alias_or_raw(store, entity_type: str, real_value: str | None) -> str | None:
    """The alias token for a real value, or the raw value if it was never aliased.

    Returning the raw value on a miss is deliberate: it hands the miss to the
    lint, which flags a non-token identifying field and refuses the projection,
    rather than export quietly papering over an inconsistent store."""
    if real_value is None:
        return None
    token = store.mapping.resolve_token(entity_type, real_value)
    return token if token is not None else real_value


def _record_from_row(store, row: dict) -> AliasRecord:
    """Reconstruct one alias-space record from a raw transaction row.

    Identifying fields become their canonical tokens (account, institution,
    payee); amount and date stay real. ``ttype``/``category`` are not persisted on
    the raw row, so they default empty — non-identifying, so the projection stays
    lint-clean and off-machine-safe regardless."""
    return AliasRecord(
        account=_alias_or_raw(store, "ACCT", row["account_id"]) or "",
        institution=_alias_or_raw(store, "INST", row.get("institution")) or "",
        payee=_payee_token(store, row["raw_payee"]),
        date=row["posted_date"],
        amount=row["amount_cents"] / 100,
    )


def store_months(store) -> list[str]:
    """Every ``YYYY-MM`` month with at least one raw transaction, sorted."""
    months = {
        row["posted_date"][:7]
        for row in store.raw_transactions()
        if row["posted_date"]
    }
    return sorted(months)


def build_month_projection(store, month: str) -> AliasSpaceProjection:
    """Rebuild the alias-space projection for one ``YYYY-MM`` month from the store.

    ISO dates sort lexically, so a prefix match scopes the month. Rows are read in
    id order, so the projection is deterministic."""
    records = [
        _record_from_row(store, row)
        for row in store.raw_transactions()
        if (row["posted_date"] or "").startswith(month)
    ]
    return AliasSpaceProjection(records)


def _attestation(verdict: str, *, now: datetime | None = None) -> dict:
    """Build the provenance attestation block (the cross-repo contract shape).

    Keys — ``verdict`` / ``timestamp`` / ``linter`` / ``linter_version`` — match
    sakuma-finance's ``records_import.read_attestation``. See the contract note in
    docs/sanitized-context-api.md; keep the two in step via ``LINTER_VERSION``."""
    stamp = (now or datetime.now(UTC)).isoformat()
    return {
        "verdict": verdict,
        "timestamp": stamp,
        "linter": LINTER_NAME,
        "linter_version": LINTER_VERSION,
    }


def export_month(
    store,
    month: str,
    out_path: str | Path,
    *,
    detector=None,
    now: datetime | None = None,
) -> ExportResult:
    """Export one month: build → lint → (green) write attested, (red) refuse.

    Returns an :class:`ExportResult`. On green the file at *out_path* holds the
    projection envelope plus a ``provenance`` attestation block. On red nothing is
    written and the findings are returned for a local-render report."""
    projection = build_month_projection(store, month)
    findings = lint_projection(projection, detector=detector)
    if findings:
        return ExportResult(
            month=month,
            row_count=len(projection.records),
            verdict="red",
            findings=findings,
            path=None,
        )

    # Green: compose the envelope with the attestation and write it. The
    # projection is alias-space only, so this file is safe to ship (crown-jewel
    # rule) — and now provably so, per the attestation the importer checks.
    envelope = projection.to_dict()
    envelope["provenance"] = _attestation(VERDICT_GREEN, now=now)
    path = Path(out_path)
    path.write_text(json.dumps(envelope, indent=2, sort_keys=True), encoding="utf-8")
    return ExportResult(
        month=month,
        row_count=len(projection.records),
        verdict=VERDICT_GREEN,
        findings=[],
        path=path,
    )
