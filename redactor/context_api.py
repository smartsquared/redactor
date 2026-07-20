"""Sanitized-context API — the contract sakuma-finance consumes (story S2.3).

This is the stable seam between the privacy gateway and its first consumer. A
bookkeeping app (sakuma-finance) never touches raw data or the mapping table; it
holds a conversation *entirely in alias-space* by going through this client:

  1. **Request alias-space context.** :meth:`SanitizedContextClient.context`
     returns a :class:`ContextWindow` — the redacted transactions (identifying
     fields are tokens, amounts and dates real), per-account balances, and the
     date-range bounds that scoped them. It is built from the ingest projection
     (``redactor.projection``), so by construction it holds no real identifier
     and is safe to serialize into model context.
  2. **Send conversation turns through the proxy.** :meth:`send` forward-redacts
     a user turn with the S2.1 outbound redactor before it can leave the machine
     — real mentions become tokens, unknown entities surface as warnings rather
     than silent leaks.
  3. **Receive alias-space replies.** :meth:`receive` wraps a model's reply as an
     alias-space turn. It deliberately does **not** un-redact: un-redaction is
     the local lens (S2.2), a separate step confined to the render boundary
     (CLAUDE.md — "un-redaction only at the local render boundary"). Keeping the
     consumer in alias-space is the whole point of the contract; its own
     transcript and any artifact it writes therefore stay redacted.

Versioning. The envelope carries :data:`CONTEXT_API_VERSION` so the consumer can
negotiate shape changes; it tracks the projection envelope version it wraps.

Crown-jewel rule. This module exposes no serializer for the mapping table and
never writes real values anywhere. The client is constructed from an alias-space
projection plus an outbound redactor; the only real values in play are the
outbound *surface forms* the redactor holds in memory solely to find and replace
them (see ``redactor.outbound``). Nothing here reverses an alias.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from redactor.outbound import LeakWarning, OutboundRedactor, Substitution
from redactor.projection import PROJECTION_VERSION, AliasRecord, AliasSpaceProjection

# The versioned contract. Bump on any shape change to ``ContextWindow`` /
# ``Turn`` so a consumer can detect and negotiate it. Tracks the alias-space
# projection envelope this API wraps (docs/sanitized-context-api.md §Versioning).
CONTEXT_API_VERSION = PROJECTION_VERSION

# Turn directions. Alias-space labels only — never a real identity.
OUTBOUND = "user->model"   # a user turn, forward-redacted before it leaves
INBOUND = "model->user"    # a model reply, still in alias-space (not un-redacted)


@dataclass(frozen=True)
class AccountBalance:
    """Aggregate figures for one account, in alias-space.

    The account and institution are tokens; the money figures are real (amounts
    are never aliased — alias-contract §1.1). ``net`` is the signed sum of the
    account's transaction amounts over the context window (negative = net out).
    """

    account: str        # ACCT-n
    institution: str    # INST-n
    net: float          # signed sum of amounts (real)
    debits: float       # sum of money-out amounts (<= 0), real
    credits: float      # sum of money-in amounts (>= 0), real
    txn_count: int
    currency: str = "USD"


@dataclass(frozen=True)
class ContextWindow:
    """The alias-space bookkeeping context handed to the consumer.

    Everything here is off-machine-safe: identifying fields are tokens, money and
    dates are real. ``start`` / ``end`` record the inclusive ISO date bounds that
    scoped the window (``None`` = unbounded on that side).
    """

    records: list[AliasRecord]
    balances: list[AccountBalance]
    start: str | None = None
    end: str | None = None
    version: int = CONTEXT_API_VERSION

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "start": self.start,
            "end": self.end,
            "records": [asdict(r) for r in self.records],
            "balances": [asdict(b) for b in self.balances],
        }

    def to_json(self, *, indent: int | None = None) -> str:
        """Serialize the window. Safe: alias-space only, no crown jewel."""
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


@dataclass(frozen=True)
class Turn:
    """One conversation turn that passed through the proxy.

    For an outbound (user->model) turn, ``text`` is the forward-redacted,
    safe-to-send string, ``substitutions`` the real mentions that became tokens,
    and ``warnings`` any unresolved entity surfaced instead of silently leaked.
    For an inbound (model->user) turn, ``text`` is the model's alias-space reply,
    left untouched — un-redaction is the separate local lens.
    """

    role: str
    text: str
    substitutions: list[Substitution] = field(default_factory=list)
    warnings: list[LeakWarning] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True iff nothing unresolved was surfaced — no warning to act on."""
        return not self.warnings


class SanitizedContextClient:
    """The typed client sakuma-finance consumes (story S2.3).

    Construct one from an alias-space projection (the ingest output) plus an
    outbound redactor built from the same store + resolver. The client keeps the
    consumer in alias-space: it serves redacted context and forward-redacts
    outbound turns, and it never un-redacts.
    """

    def __init__(
        self,
        *,
        projection: AliasSpaceProjection,
        redactor: OutboundRedactor,
    ) -> None:
        self._records = list(projection.records)
        self._redactor = redactor
        self._version = projection.version

    # -- alias-space context -------------------------------------------------- #

    def context(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        accounts: list[str] | None = None,
    ) -> ContextWindow:
        """Return the alias-space context window.

        Filters the ingested transactions to an inclusive ISO date range
        (``start``/``end``, either side optional) and an optional set of account
        tokens, then computes per-account balances over the selection. ISO dates
        sort lexically, so the bounds compare as plain strings.
        """
        acct_filter = set(accounts) if accounts is not None else None
        records = [
            r
            for r in self._records
            if (start is None or r.date >= start)
            and (end is None or r.date <= end)
            and (acct_filter is None or r.account in acct_filter)
        ]
        return ContextWindow(
            records=records,
            balances=self._balances(records),
            start=start,
            end=end,
            version=self._version,
        )

    @staticmethod
    def _balances(records: list[AliasRecord]) -> list[AccountBalance]:
        """Aggregate signed amounts per (account, institution) token pair."""
        acc: dict[str, dict] = {}
        for r in records:
            b = acc.setdefault(
                r.account,
                {"institution": r.institution, "net": 0.0, "debits": 0.0,
                 "credits": 0.0, "count": 0},
            )
            b["net"] += r.amount
            if r.amount < 0:
                b["debits"] += r.amount
            else:
                b["credits"] += r.amount
            b["count"] += 1
        return [
            AccountBalance(
                account=account,
                institution=b["institution"],
                net=round(b["net"], 2),
                debits=round(b["debits"], 2),
                credits=round(b["credits"], 2),
                txn_count=b["count"],
            )
            for account, b in sorted(acc.items())
        ]

    # -- conversation turns --------------------------------------------------- #

    def send(self, user_text: str) -> Turn:
        """Forward-redact a user turn (S2.1) into a safe-to-send outbound turn.

        Real mentions are replaced by their stable tokens; an entity that
        resolves to nothing known is surfaced in ``warnings`` — never a silent
        leak. Inspect ``turn.clean`` before relaying the text to a model.
        """
        result = self._redactor.redact(user_text)
        return Turn(
            role=OUTBOUND,
            text=result.text,
            substitutions=result.substitutions,
            warnings=result.warnings,
        )

    def receive(self, model_text: str) -> Turn:
        """Wrap a model's alias-space reply as an inbound turn.

        The text is returned untouched, still in alias-space. This client never
        un-redacts: rendering tokens back to real values is the local lens (S2.2,
        ``redactor.lens`` / ``redactor lens``), a distinct step at the render
        boundary where the mapping table lives. Keeping ``receive`` in
        alias-space is what lets the consumer's own transcript and artifacts stay
        redacted (CLAUDE.md — un-redaction only at the local render boundary).
        """
        return Turn(role=INBOUND, text=model_text)
