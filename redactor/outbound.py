"""Outbound forward-redaction — the user→model half of the proxy (story S2.1).

The gateway is a **bidirectional** alias proxy (docs/brief.md). Inbound
(model→human) it substitutes alias tokens back to real strings at the local
render boundary (S2.2). This module is the *outbound* half: before a user
utterance can leave the machine toward a model, any mention of a real entity is
forward-redacted to its stable alias token — because if the user types "Whole
Foods" and it goes out verbatim, they have broken alias-space themselves
(brief, un-redaction mechanism).

Two responsibilities, matching the acceptance criteria of issue #9:

  * **Redact known entities correctly.** A mention is resolved against the
    mapping table (S1.2, the crown jewel) *plus* entity resolution (S1.3): the
    resolver collapses "Whole Foods" / "WHOLEFDS #1029 SEA" / "WF MKT 445" onto
    one payee entity, and the mapping table gives that entity its ``PAYEE-n``
    token. Institutions and accounts match on their exact stored real strings.
  * **Never silently leak an unknown entity.** A mention that resolves to
    nothing known is *surfaced as a warning* rather than passed through
    unnoticed. The text still carries the unknown span (the proxy does not
    invent an alias it cannot reverse), but the local caller is told, loudly, so
    the decision is never silent.

Direction of caution is the mirror image of the inbound matcher. Inbound
(contract §3.2) must be **never-false-positive** — substituting a non-token
would corrupt model prose. Outbound favours the *safe* error instead: when a
surface form collides with an ordinary word (a merchant literally named
"Target"), over-redaction costs a little conversational fidelity, while
under-redaction is an identifier leak. We take the redaction.

This module runs only where the mapping table lives, and holds real surface
forms in memory solely to *find* them in outbound text. It exposes no serializer
and never writes those strings anywhere — the crown-jewel rule (CLAUDE.md) still
holds: the only persistence sink for the mapping is the encrypted store.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from redactor.alias import find_aliases
from redactor.resolve import PayeeResolver, normalize

# Entity classes whose real values we match as exact literal strings (the store
# holds their real form: an institution name, an account id). PAYEE is handled
# separately, through entity resolution, because a user names a merchant in free
# form, not by its memo garbage.
_LITERAL_CLASSES = ("INST", "ACCT", "PERSON", "CARD")

# A mention -> its PAYEE alias token, or None if it resolves to no known payee.
PayeeMatcher = Callable[[str], "str | None"]

# Ordinary capitalised words that begin sentences or fill bookkeeping questions.
# Used only by the unknown-entity heuristic to avoid warning on normal prose;
# it is deliberately conservative about what counts as a suspected entity.
_COMMON = frozenset(
    w.upper()
    for w in """
    a an the this that these those and or but nor so yet for
    how what when where why who which whose whom
    i me my mine we us our ours you your yours he him his she her it its
    they them their theirs
    is are was were am be been being do does did done has have had having
    can could shall should will would may might must need
    to of in on at by with from into onto over under about as than then out up
    show tell give list find get see check compare
    spend spent spending pay paid pays buy bought buying cost costs costing
    save saved spend total totals balance balances transaction transactions
    much many more most less least last next this month months year years week
    weeks day days today yesterday tomorrow ago so far now recent recently
    dollars dollar usd cents amount amounts average
    january february march april may june july august september october
    november december
    monday tuesday wednesday thursday friday saturday sunday
    """.split()
)

# A suspected-entity span: a run of Title-Case words, or an ALL-CAPS token. The
# lookarounds keep it word-bounded so it never fires mid-word.
_PROPER_RUN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:[A-Z][A-Za-z0-9'&.]*)(?:\s+[A-Z][A-Za-z0-9'&.]*)*"
    r"(?![A-Za-z0-9])"
)


@dataclass(frozen=True)
class Substitution:
    """One real mention that was forward-redacted to an alias token."""

    surface: str            # the real string found in the utterance
    token: str              # the canonical alias it was replaced with
    span: tuple[int, int]   # (start, end) offsets in the *original* text


@dataclass(frozen=True)
class LeakWarning:
    """A suspected entity mention that resolved to nothing known.

    Surfaced to the local caller so an unknown entity is never a *silent* leak.
    The span is left in the outbound text (the proxy cannot alias what it cannot
    reverse); the warning is the "not silent" part.
    """

    surface: str            # the unresolved mention, as the user typed it
    span: tuple[int, int]   # (start, end) offsets in the original text
    reason: str = "unresolved-entity"


@dataclass(frozen=True)
class OutboundResult:
    """Outcome of forward-redacting one outbound utterance."""

    text: str                                     # redacted, safe-to-send text
    substitutions: list[Substitution] = field(default_factory=list)
    warnings: list[LeakWarning] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """True iff nothing unresolved was surfaced — no warning to act on."""
        return not self.warnings


def _word_bounded(alternatives: list[str]) -> re.Pattern[str] | None:
    """Compile a case-insensitive, word-bounded alternation of literal surfaces.

    Alternatives are escaped and ordered longest-first so a longer, more
    specific surface wins over a shorter prefix at the same position. Internal
    runs of whitespace are matched flexibly (``\\s+``) so "Bank of  Nowhere"
    still matches "Bank of Nowhere".
    """
    if not alternatives:
        return None
    ordered = sorted(set(alternatives), key=lambda s: (-len(s), s))
    parts = [r"\s+".join(re.escape(w) for w in s.split()) for s in ordered]
    return re.compile(
        r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])",
        re.IGNORECASE,
    )


def _norm_key(text: str) -> str:
    """Whitespace- and case-normalise a matched surface to its lexicon key."""
    return " ".join(text.split()).lower()


class OutboundRedactor:
    """Forward-redacts real entity mentions in outbound (user→model) text.

    Build one with :meth:`from_store`, or directly from a literal lexicon plus a
    payee matcher for testing. Call :meth:`redact` to get an
    :class:`OutboundResult`; :meth:`redact_text` is the string-only shortcut.
    """

    def __init__(
        self,
        *,
        literals: dict[str, str],
        payee_matcher: PayeeMatcher | None = None,
        payee_surfaces: dict[str, str] | None = None,
    ) -> None:
        # literal surface (normalised key) -> alias token, for INST/ACCT/... and
        # any exact payee surface forms derived from memos.
        self._literals = {_norm_key(k): v for k, v in literals.items()}
        # payee surface forms (normalised key) -> token, matched as literals but
        # kept separate so they can be built from entity resolution.
        self._payee_surfaces = {
            _norm_key(k): v for k, v in (payee_surfaces or {}).items()
        }
        self._payee_matcher = payee_matcher
        merged = {**self._literals, **self._payee_surfaces}
        self._matcher = _word_bounded(list(merged.keys()))
        self._lexicon = merged

    # -- construction --------------------------------------------------------- #

    @classmethod
    def from_store(cls, store, resolver: PayeeResolver) -> OutboundRedactor:
        """Build a redactor from an ingested store and its payee resolver.

        The *resolver* must be the same instance ingest resolved memos with, so
        its learned entities line up with the ``PAYEE`` keys in the mapping
        table. Institution/account real values are read from the mapping (the
        crown jewel) for literal matching; payee surface forms are reconstructed
        from the raw memos in the store and grouped by entity, so a friendly
        mention ("Whole Foods") and a memo variant ("WHOLEFDS #1029 SEA") map to
        the one token their shared entity holds.
        """
        literals: dict[str, str] = {}
        for token in store.mapping.tokens():
            hits = find_aliases(token)
            if not hits:
                continue
            cls_name = hits[0].cls
            if cls_name in _LITERAL_CLASSES:
                sealed = store.mapping.resolve_alias(token)
                if sealed is not None:
                    literals[sealed.reveal()] = token

        payee_surfaces: dict[str, str] = {}
        for txn in store.raw_transactions():
            memo = txn.get("raw_payee")
            if not memo:
                continue
            eid = resolver.match(memo)
            if eid is None:
                continue
            token = store.mapping.resolve_token("PAYEE", str(eid))
            if token is None:
                continue
            # The friendly form the user is likely to type is the memo's
            # significant-token phrase ("WHOLE FOODS"); the raw memo covers a
            # pasted statement line verbatim.
            phrase = " ".join(normalize(memo))
            if phrase:
                payee_surfaces[phrase] = token
            payee_surfaces[memo] = token

        def payee_matcher(mention: str) -> str | None:
            eid = resolver.match(mention)
            if eid is None:
                return None
            return store.mapping.resolve_token("PAYEE", str(eid))

        return cls(
            literals=literals,
            payee_surfaces=payee_surfaces,
            payee_matcher=payee_matcher,
        )

    # -- redaction ------------------------------------------------------------ #

    def redact(self, text: str) -> OutboundResult:
        """Forward-redact *text*, returning the safe-to-send string plus the
        substitutions made and warnings for anything unresolved."""
        subs = self._match_literals(text)
        covered = [(s.span[0], s.span[1]) for s in subs]
        warnings = self._warn_unresolved(text, covered)

        out = self._apply(text, subs)
        subs.sort(key=lambda s: s.span)
        warnings.sort(key=lambda w: w.span)
        return OutboundResult(text=out, substitutions=subs, warnings=warnings)

    def redact_text(self, text: str) -> str:
        """Convenience wrapper returning only the redacted text."""
        return self.redact(text).text

    # -- internals ------------------------------------------------------------ #

    def _match_literals(self, text: str) -> list[Substitution]:
        if self._matcher is None:
            return []
        out: list[Substitution] = []
        for m in self._matcher.finditer(text):
            token = self._lexicon.get(_norm_key(m.group(0)))
            if token is None:  # pragma: no cover - key always present by build
                continue
            out.append(Substitution(surface=m.group(0), token=token, span=m.span()))
        return out

    def _warn_unresolved(
        self, text: str, covered: list[tuple[int, int]]
    ) -> list[LeakWarning]:
        """Flag Title-Case / ALL-CAPS mentions that survived redaction.

        A span that overlaps an already-redacted substitution is skipped (it was
        a known entity). A span made only of ordinary words is skipped. What
        remains is an entity-shaped mention we could not resolve — surfaced so it
        is never a silent leak. As a last line of defence the payee matcher is
        consulted too: if it *does* resolve, the caller simply typed a form our
        literal lexicon missed, and we do not warn.
        """
        warnings: list[LeakWarning] = []
        for m in _PROPER_RUN.finditer(text):
            span = m.span()
            if any(span[0] < c_end and c_start < span[1] for c_start, c_end in covered):
                continue
            surface = m.group(0)
            words = [w for w in re.split(r"[^A-Za-z0-9]+", surface) if w]
            if all(w.upper() in _COMMON for w in words):
                continue
            if self._payee_matcher is not None and self._payee_matcher(surface):
                continue
            warnings.append(LeakWarning(surface=surface, span=span))
        return warnings

    @staticmethod
    def _apply(text: str, subs: list[Substitution]) -> str:
        """Rebuild *text* with each substitution's span replaced by its token."""
        if not subs:
            return text
        parts: list[str] = []
        cursor = 0
        for s in sorted(subs, key=lambda s: s.span):
            start, end = s.span
            parts.append(text[cursor:start])
            parts.append(s.token)
            cursor = end
        parts.append(text[cursor:])
        return "".join(parts)
