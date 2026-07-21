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

from collections.abc import Callable, Iterable

from redactor.alias import canonical, find_aliases
from redactor.fixtures import Statement
from redactor.projection import AliasRecord, AliasSpaceProjection

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
) -> list[AliasRecord]:
    """Ingest one statement: raw rows to the store, alias records returned."""
    account_token = _issue_alias(store, "ACCT", statement.account_id)
    institution_token = _issue_alias(store, "INST", statement.institution)

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
        )
        payee_token = _issue_alias(store, "PAYEE", resolve_payee(txn.description))
        records.append(
            AliasRecord(
                account=account_token,
                institution=institution_token,
                payee=payee_token,
                date=txn.date,
                amount=txn.amount,
                ttype=txn.ttype,
                category=txn.category,
            )
        )
    return records


def ingest_statements(
    store,
    statements: Iterable[Statement],
    *,
    resolve_payee: PayeeResolver = conservative_payee_key,
) -> AliasSpaceProjection:
    """Ingest many statements end-to-end.

    Raw transactions are persisted to *store*; the return value is the combined
    alias-space projection — a separate, off-machine-safe output.
    """
    records: list[AliasRecord] = []
    for statement in statements:
        records.extend(ingest_statement(store, statement, resolve_payee=resolve_payee))
    return AliasSpaceProjection(records)
