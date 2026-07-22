"""Detection-based leak-lint (story S0.2): prove an artifact clean by detection.

The wave-1 leak-lint (:mod:`redactor.leaklint`) is *seed-based*: it needs the
fixture manifest to know what a real identifier looks like. Real data has no
manifest — no ground truth to diff against — so this lint proves an alias-space
projection or artifact clean the only way that survives contact with real data:
by **detecting** identifiers rather than comparing to a known set of seeds.

Two pillars, both required (the story):

  * **The finance Detector (Presidio).** :func:`redactor.detect.finance_detector`
    with no manifest still catches account-id-shaped spans and 9-digit routing
    runs by pattern. A caller may pass a Detector seeded with a deny-list of
    known entity names for stronger coverage; the standalone lint needs none.
  * **Structural checks.** The fake-ness validators (:mod:`redactor.fakeness`)
    flag anything checksum-*valid* — a real ABA routing number, a Luhn-valid
    card/account number, an SSN-shaped string — and **alias-contract
    conformance** flags any identifying field that is not a canonical alias
    token (a raw merchant name left in a ``payee`` slot, say).

A correctly-aliased projection carries only ``CLASS-n`` tokens, real amounts and
real dates, so this returns nothing. Like :mod:`redactor.leaklint` it touches no
mapping-table state: it scans text and structure, and knows only the shapes a
real identifier takes — never a binding. That keeps it safe to run standalone
(``redactor lint <file>``) on the crown-jewel-free side of the boundary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from redactor.alias import find_aliases
from redactor.detect import finance_detector
from redactor.fakeness import scan_text

if TYPE_CHECKING:  # pragma: no cover - typing only
    from redactor.detect import Detector
    from redactor.projection import AliasSpaceProjection

# Identifying fields in a serialized projection record (docs/alias-contract.md
# §1.1). These MUST carry a canonical alias token; anything else is a leak. The
# non-identifying fields (date, amount, ttype, category) stay real by contract
# and are scanned only by the text pillar, never for token conformance.
_IDENTIFYING_FIELDS: tuple[str, ...] = ("account", "institution", "payee")


@dataclass(frozen=True)
class LintFinding:
    """One reason an artifact is not clean.

    ``kind`` is the detector entity type (``ACCOUNT_ID``, ``ROUTING_NUMBER``,
    ...), a structural checksum kind (``aba_routing`` | ``luhn_card`` | ``ssn``),
    or ``alias_nonconformance`` for an identifying field that is not a token.
    """

    kind: str
    match: str       # the offending substring / field value
    detail: str = ""  # where / how it was found (position, field name, ...)


def _is_canonical_token(value: str) -> bool:
    """True iff *value* is exactly one canonical alias token and nothing else."""
    matches = find_aliases(value)
    return len(matches) == 1 and matches[0].canonical == value


def lint_text(text: str, *, detector: Detector | None = None) -> list[LintFinding]:
    """Return every leak the detection + structural pillars find in *text*.

    Runs the finance Detector and the fake-ness structural checks over the raw
    text. This is the pillar used for free-form artifacts (a PR body, a report)
    where there is no record structure to check for token conformance.
    """
    findings: list[LintFinding] = []

    # Structural: checksum-valid routing / card numbers, SSN-shaped strings.
    for f in scan_text(text):
        findings.append(LintFinding(f.kind, f.match, f"{f.line}:{f.col}"))

    # Detection: account-id / routing patterns (plus any deny-list the caller
    # seeded the Detector with). Pattern-based, so no manifest is required.
    det = detector if detector is not None else finance_detector()
    for e in det.detect(text):
        findings.append(
            LintFinding(e.entity_type, e.text, f"{e.start}:{e.end} score={e.score:.2f}")
        )

    return _dedupe(findings)


def _conformance_findings(records: list) -> list[LintFinding]:
    """Alias-contract conformance: identifying fields must be canonical tokens.

    A raw merchant name, account id, or institution name left in an identifying
    slot is a leak even when it dodges every checksum and pattern — this is what
    catches "detected names" that the structural pillar alone would miss.
    """
    findings: list[LintFinding] = []
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        for field in _IDENTIFYING_FIELDS:
            value = record.get(field)
            if value is None:
                continue
            if not isinstance(value, str) or not _is_canonical_token(value):
                findings.append(
                    LintFinding(
                        "alias_nonconformance",
                        str(value),
                        f"records[{i}].{field} is not a canonical alias token",
                    )
                )
    return findings


def lint_projection_dict(
    data: dict, *, detector: Detector | None = None
) -> list[LintFinding]:
    """Lint a projection already in dict form: text pillars + token conformance."""
    findings = lint_text(json.dumps(data, sort_keys=True), detector=detector)
    records = data.get("records")
    if isinstance(records, list):
        findings.extend(_conformance_findings(records))
    return _dedupe(findings)


def lint_projection(
    projection: AliasSpaceProjection, *, detector: Detector | None = None
) -> list[LintFinding]:
    """Lint an :class:`~redactor.projection.AliasSpaceProjection` end to end."""
    return lint_projection_dict(projection.to_dict(), detector=detector)


def lint_file(path: str | Path, *, detector: Detector | None = None) -> list[LintFinding]:
    """Lint an artifact file. If it parses as a projection, also check conformance.

    Free-form artifacts get the detection + structural text pillars. A file that
    parses as a projection envelope (a dict with a ``records`` list) additionally
    gets alias-contract conformance on its identifying fields.
    """
    text = Path(path).read_text(encoding="utf-8")
    findings = lint_text(text, detector=detector)

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("records"), list):
        findings.extend(_conformance_findings(data["records"]))

    return _dedupe(findings)


def _dedupe(findings: list[LintFinding]) -> list[LintFinding]:
    """Drop exact-duplicate findings, preserving first-seen order.

    Both pillars can flag the same span (a checksum-valid 9-digit run is both a
    structural ``aba_routing`` hit and a pattern ``ROUTING_NUMBER`` hit); those
    are distinct kinds and both kept. Only byte-identical findings collapse.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[LintFinding] = []
    for f in findings:
        key = (f.kind, f.match, f.detail)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def format_findings(findings: list[LintFinding]) -> str:
    """Human-readable, alias-space-safe rendering of findings for a terminal.

    Findings echo the offending substring by necessity (that is the point of a
    lint). This is the local render surface; callers must not ship the output
    off-machine.
    """
    if not findings:
        return "redactor lint: clean — no identifiers detected."
    lines = [f"redactor lint FAILED — {len(findings)} finding(s):"]
    for f in findings:
        detail = f" ({f.detail})" if f.detail else ""
        lines.append(f"  {f.kind}: {f.match}{detail}")
    return "\n".join(lines)
