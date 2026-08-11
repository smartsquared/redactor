"""Statement-file ingest adapter (story S1.4).

Turns statement files (CSV + OFX) into two artifacts, cleanly separated:

  1. **Raw transactions** — every field as the bank rendered it — written to the
     local SQLCipher store (``redactor.store``). This is the only place raw data
     lands. It never leaves the machine.
  2. An **alias-space projection** (``redactor.projection``) — the redacted,
     off-machine-safe view: identifying fields replaced by canonical alias
     tokens, amounts and dates left real. This is a *separate output*, returned
     to the caller; it is what may be shipped into model context.

The adapter runs the wave-1 pipeline at ingest:

  * **Detection (S1.1).** Every identifying string in a structured statement is
    structurally known: the account id and institution (statement metadata) and
    the counterparty (the memo). The projection replaces the *whole* memo with a
    ``PAYEE`` token, so any identifier a detector could find embedded in memo
    text (a stray account/routing/card number) is dropped wholesale rather than
    carried forward — the strongest guarantee. Presidio's free-text NER (S1.1)
    is what the *outbound* proxy (S2.1) needs; the structured projection here
    stays clean without it.
  * **Alias assignment (S1.2).** Stable issuance against the S0.3 mapping table:
    same real entity → same token forever; a new entity → the next unused
    integer for its class (contract §2). Implemented as CRUD over
    ``store.mapping`` — no new off-store sink.
  * **Payee resolution (S1.3).** Pluggable via ``resolve_payee``. The default is
    deliberately **conservative** — it groups only case/whitespace-identical
    memo strings, so it can never false-merge two distinct merchants (contract
    §2.5). Cross-variant resolution ("WF MKT 445" ≡ "WHOLEFDS #1029 SEA") is the
    S1.3 moat (#6); inject its resolver here when it lands.

Nothing in this module serializes the mapping table off-store: it reads/writes
only through the encrypted ``store.mapping`` connection and hands back an
alias-space projection that holds no real values.
"""
from __future__ import annotations

import sys
from collections.abc import Callable, Iterable

from redactor.alias import canonical, find_aliases
from redactor.fixtures import Statement
from redactor.lint import LintFinding, format_findings, lint_projection
from redactor.projection import AliasRecord, AliasSpaceProjection


class LintFailure(Exception):
    """A projection failed the detection-based leak-lint (story S0.2).

    Raised as ingest's mandatory final step when a projection carries anything
    real-looking. Carries the findings so a caller can render them locally; the
    exception message stays alias-space-agnostic (it does not itself echo the
    offending values, which live on ``findings``).
    """

    def __init__(self, findings: list[LintFinding]):
        self.findings = findings
        super().__init__(
            f"projection failed leak-lint with {len(findings)} finding(s); "
            f"refusing to emit (pass allow_leaks=True to override)"
        )


# ANSI red, used only to make the override warning loud on a local terminal.
# This is stderr on the user's own machine — never part of an off-machine
# artifact — so a control code here does not cross the privacy boundary.
_RED = "\x1b[31m"
_RESET = "\x1b[0m"


def _lint_or_refuse(
    projection: AliasSpaceProjection, *, allow_leaks: bool
) -> AliasSpaceProjection:
    """Ingest's mandatory final step: lint the projection before it can escape.

    Zero findings → the projection is returned unchanged. Findings → refuse by
    raising :class:`LintFailure`, unless *allow_leaks* is set, in which case the
    projection is returned but a loud red warning is printed to stderr so the
    override is never silent.
    """
    findings = lint_projection(projection)
    if not findings:
        return projection
    if not allow_leaks:
        raise LintFailure(findings)
    print(
        f"{_RED}redactor: WARNING — emitting a projection that FAILED the "
        f"leak-lint (allow_leaks override).{_RESET}\n{format_findings(findings)}",
        file=sys.stderr,
    )
    return projection

# The S1.3 seam: a memo string -> the key that identifies its payee entity. Same
# key => same PAYEE alias. The key is stored verbatim as the mapping's real value
# and is what the lens reveals at the render boundary, so it must be a
# human-readable name, not an opaque internal id (issue #25): the S1.3 resolver
# supplies ``PayeeResolver.resolve_display`` for exactly this.
PayeeResolver = Callable[[str], str]


def conservative_payee_key(description: str) -> str:
    """Default payee resolver: case- and whitespace-normalized exact match.

    Groups only memo strings that are identical modulo case and surrounding
    whitespace. This never coerces a merge between distinct strings, so it is
    false-merge-free by construction (contract §2.5). Real cross-variant
    resolution is story S1.3.
    """
    return " ".join(description.split()).upper()


def _next_number(store, entity_type: str) -> int:
    """Next unused integer for *entity_type*: max issued + 1 (contract §2.3).

    Monotonic and gap-tolerant; never reuses a number, so a stale redacted
    record can never be silently re-bound to a different entity.
    """
    highest = 0
    for token in store.mapping.tokens():
        for match in find_aliases(token):
            if match.cls == entity_type and match.n > highest:
                highest = match.n
    return highest + 1


def _issue_alias(store, entity_type: str, real_value: str) -> str:
    """Return the stable alias token for a real value, issuing one if new.

    Bijective within a class: an existing binding is reused (stability forever),
    a new entity gets the next token. This is the S1.2 CRUD path over the S0.3
    mapping table.
    """
    existing = store.mapping.resolve_token(entity_type, real_value)
    if existing is not None:
        return existing
    token = canonical(entity_type, _next_number(store, entity_type))
    store.mapping.put(token, entity_type, real_value)
    return token


def _amount_cents(amount: float) -> int:
    return int(round(amount * 100))


def ingest_statement(
    store,
    statement: Statement,
    *,
    resolve_payee: PayeeResolver = conservative_payee_key,
    registry=None,
    lint: bool = True,
    allow_leaks: bool = False,
) -> list[AliasRecord]:
    """Ingest one statement: raw rows to the store, alias records returned.

    Payee aliasing has two seams. By default the conservative *resolve_payee*
    keyer is used (exact-match, false-merge-free). Pass a
    :class:`~redactor.payees.PayeeRegistry` as *registry* to run the story-S1.1
    entity-resolution flow instead: it groups memo variants onto one stable
    ``PAYEE`` token, persists the grouping + confidence, and flags low-confidence
    groupings for review — the path the ``redactor ingest`` CLI wires up.

    Runs the detection-based leak-lint (story S0.2) as its final step unless
    *lint* is off. A failing projection is refused (``LintFailure``) unless
    *allow_leaks* is set, which emits it with a loud red warning instead.
    """
    account_token = _issue_alias(store, "ACCT", statement.account_id)
    # A bare CSV export names no institution; don't mint an INST alias for "".
    # The projection carries None (unknown) instead — lint treats None as
    # unknown but any non-token string, "" included, as non-conforming.
    institution_token = (
        _issue_alias(store, "INST", statement.institution) if statement.institution else None
    )

    # Document-frequency pre-pass (issue #37): teach the entity resolver how
    # widely each memo token is spread across this whole statement before any
    # grouping decision is made, so ubiquitous transaction filler (VISA, POS, a
    # big-city name) is recognised as non-identifying from the first row rather
    # than snowballing distinct merchants together while frequency warms up.
    if registry is not None:
        for txn in statement.transactions:
            registry.observe(txn.description)

    records: list[AliasRecord] = []
    for txn in statement.transactions:
        # Raw fields land ONLY in the encrypted store.
        store.add_raw_transaction(
            account_id=statement.account_id,
            posted_date=txn.date,
            amount_cents=_amount_cents(txn.amount),
            raw_payee=txn.description,
            raw_memo=None,
            source_file=statement.source.name,
            # Persist the account -> institution link so `redactor export` can
            # reconstruct the INST token offline from the store (issue #41).
            institution=statement.institution,
        )
        if registry is not None:
            payee_token = registry.record(txn.description)
        else:
            payee_token = _issue_alias(store, "PAYEE", resolve_payee(txn.description))
        # An earlier-tagged payee carries its closed-vocabulary tag into this
        # projection too (tags live on the canonical head; a first ingest has
        # none, so this is "" until the human runs the categorize flow).
        head = store.mapping.canonical_head(payee_token) or payee_token
        records.append(
            AliasRecord(
                account=account_token,
                institution=institution_token,
                payee=payee_token,
                date=txn.date,
                amount=txn.amount,
                ttype=txn.ttype,
                category=txn.category,
                payee_category=store.payee_category(head) or "",
            )
        )
    if lint:
        _lint_or_refuse(AliasSpaceProjection(records), allow_leaks=allow_leaks)
    return records


def ingest_statements(
    store,
    statements: Iterable[Statement],
    *,
    resolve_payee: PayeeResolver = conservative_payee_key,
    lint: bool = True,
    allow_leaks: bool = False,
) -> AliasSpaceProjection:
    """Ingest many statements end-to-end.

    Raw transactions are persisted to *store*; the return value is the combined
    alias-space projection — a separate, off-machine-safe output.

    The combined projection passes through the detection-based leak-lint (story
    S0.2) as the mandatory final step before it is returned: a projection with
    any real-looking identifier is refused (``LintFailure``) unless *allow_leaks*
    is set, which emits it with a loud red warning. Per-statement linting is
    skipped here so the authoritative check runs once over the whole output.
    """
    records: list[AliasRecord] = []
    for statement in statements:
        records.extend(
            ingest_statement(store, statement, resolve_payee=resolve_payee, lint=False)
        )
    projection = AliasSpaceProjection(records)
    if lint:
        _lint_or_refuse(projection, allow_leaks=allow_leaks)
    return projection
