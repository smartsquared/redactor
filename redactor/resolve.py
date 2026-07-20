"""Payee entity resolution — the moat (story S1.3).

Bank memo strings are garbage: the same merchant shows up as ``WHOLEFDS #1029
SEA`` one month and ``WF MKT 445 SEATTLE WA`` the next. This module collapses
those variants onto one stable *payee entity* so that alias assignment (S1.2)
can hand the whole family a single ``PAYEE-n`` token that stays put across
months (docs/alias-contract.md §2.4). Entity ids issued here are the identity
alias assignment keys on.

The resolver is deliberately layered, cheapest-and-safest first:

1. **Deterministic normalization.** Strip the non-identifying noise — store
   numbers, cities, US state codes, dates, and generic transaction filler —
   down to the discriminative *significant tokens*. Two memos that share a
   significant token (e.g. ``COSTCO``) resolve together with full confidence.
2. **Fuzzy layer, behind a confidence threshold.** For the leftovers where the
   brand itself was mangled (``WHOLEFDS`` vs ``WHOLE FOODS``, ``WF`` vs
   ``WHOLE FOODS``), score prefix / acronym / character similarity and merge
   only above :data:`DEFAULT_THRESHOLD`.
3. **Below threshold → a new entity.** Resolution never guesses silently: a memo
   that does not clear the bar starts its own entity rather than being coerced
   into an existing one. A false merge is a re-identification bug
   (docs/alias-contract.md §2.5), so the algorithm is tuned to prefer a stray
   new entity over ever fusing two distinct merchants.

This module holds no real data and never touches the mapping table — it works on
memo *strings* only and returns opaque integer entity ids. Binding those ids to
``PAYEE-n`` aliases (and persisting them) is the crown-jewel path in S1.2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# The confidence a fuzzy match must clear to join an existing entity. Chosen so
# the deterministic signals (shared significant token = 1.0) always pass while
# the genuinely-ambiguous acronym cases (``SBUX`` vs ``STARBUCKS``) fall below
# it and correctly start their own entity instead of risking a false merge.
DEFAULT_THRESHOLD = 0.80

# Minimum length for a prefix-containment fuzzy match, e.g. ``WHOLE`` ⊂
# ``WHOLEFDS``. Long enough that ordinary short brand fragments cannot collide.
_MIN_PREFIX = 5

# Character-similarity floor for the token-level fuzzy signal (typo tolerance).
_MIN_CHAR_SIM = 0.86

# The two-letter US state codes — geographic noise, never a payee signal.
_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)

# Non-discriminative filler: city fragments that recur in the fixtures plus the
# generic transaction vocabulary banks bolt onto memos (store types, channels,
# payment words). Stripping these keeps the *brand* tokens as the entity anchor.
# It is deliberately conservative — only words that carry no merchant identity.
_STOPWORDS = frozenset(
    """
    SEATTLE SEA KENT LOS GATOS SAN FRANCISCO
    MKT MARKET STORE WHSE WHOLESALE COM US BILL ONLINE ORDER OIL GAS SERVICE
    STATION CABLE COMM UTILITY PMT PAYMENT MEXICAN GRILL TRIP HELP EATS MKTP QPS
    CORP PAYROLL PPD PROPERTY MGMT RENT AUTOPAY THANK YOU INC CO LLC LTD THE
    """.split()
)

# Month abbreviations — the non-numeric half of a date fragment.
_MONTHS = frozenset(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP SEPT OCT NOV DEC".split()
)


def normalize(memo: str) -> list[str]:
    """Reduce a raw memo to its ordered *significant tokens*.

    Uppercases, splits on non-alphanumerics, then drops the noise that carries
    no merchant identity: any token containing a digit (store numbers, txn
    codes, and all numeric date fragments), US state codes, month abbreviations,
    single letters, and the generic filler vocabulary. Order is preserved so the
    fuzzy layer can compute acronyms.

    ``"WHOLEFDS #1029 SEA"`` -> ``["WHOLEFDS"]``;
    ``"WHOLE FOODS MKT #10029"`` -> ``["WHOLE", "FOODS"]``.
    """
    tokens: list[str] = []
    for raw in re.split(r"[^A-Za-z0-9]+", memo.upper()):
        if not raw:
            continue
        if any(c.isdigit() for c in raw):  # store #, txn code, numeric date
            continue
        if len(raw) == 1:
            continue
        if raw in _US_STATES or raw in _MONTHS or raw in _STOPWORDS:
            continue
        tokens.append(raw)
    return tokens


def _acronym(tokens: list[str]) -> str:
    """First-letters acronym of a multi-token name, e.g. ``[WHOLE, FOODS]`` -> ``WF``."""
    return "".join(t[0] for t in tokens)


@dataclass
class _Entity:
    """One resolved payee: an id plus the accumulated evidence about it."""

    id: int
    tokens: set[str] = field(default_factory=set)  # union of significant tokens
    members: list[list[str]] = field(default_factory=list)  # per-memo token lists

    def absorb(self, tokens: list[str]) -> None:
        self.tokens.update(tokens)
        self.members.append(tokens)

    def acronyms(self) -> set[str]:
        """Single significant tokens that could be an acronym (e.g. ``WF``, ``TJ``)."""
        return {m[0] for m in self.members if len(m) == 1 and 2 <= len(m[0]) <= 5}

    def initialisms(self) -> set[str]:
        """Acronyms of this entity's multi-token members (e.g. ``WHOLE FOODS`` -> ``WF``)."""
        return {_acronym(m) for m in self.members if len(m) >= 2}


class PayeeResolver:
    """Online, stability-preserving payee resolver.

    Feed it memo strings in any order via :meth:`resolve`; it returns a stable
    integer entity id, issuing a fresh one only when nothing clears the
    confidence threshold. Ids are monotonic and never reused, matching the
    alias-contract issuance rule (§2.3).
    """

    def __init__(self, threshold: float = DEFAULT_THRESHOLD) -> None:
        self._threshold = threshold
        self._entities: list[_Entity] = []
        self._next_id = 1

    def entity_count(self) -> int:
        return len(self._entities)

    def resolve(self, memo: str) -> int:
        """Resolve *memo* to a payee entity id, creating one if none matches."""
        tokens = normalize(memo)
        if not tokens:
            # No significant tokens survived (pure filler like an autopay note):
            # fall back to the squashed alphabetic form so identical filler memos
            # still coalesce, but never merges into a real brand entity.
            tokens = [re.sub(r"[^A-Z]", "", memo.upper()) or "UNKNOWN"]

        matches = [
            (score, ent)
            for ent in self._entities
            if (score := self._confidence(tokens, ent)) >= self._threshold
        ]

        if not matches:
            ent = _Entity(id=self._next_id)
            self._next_id += 1
            ent.absorb(tokens)
            self._entities.append(ent)
            return ent.id

        # Join the best match, and fold in every other above-threshold entity:
        # a memo that independently matches two existing entities is fresh
        # evidence that they were the same merchant all along (this is what makes
        # resolution robust to the order memos arrive in). Sub-threshold entities
        # are untouched — no silent merges.
        matches.sort(key=lambda m: m[0], reverse=True)
        target = matches[0][1]
        target.absorb(tokens)
        for _, other in matches[1:]:
            target.tokens.update(other.tokens)
            target.members.extend(other.members)
            self._entities.remove(other)
        return target.id

    # -- scoring ---------------------------------------------------------- #
    def _confidence(self, tokens: list[str], ent: _Entity) -> float:
        """Confidence in ``tokens`` and ``ent`` being the same payee, in [0, 1]."""
        token_set = set(tokens)

        # Deterministic: a shared discriminative token is a lock.
        if token_set & ent.tokens:
            return 1.0

        # Fuzzy: a significant token contains another as a long prefix
        # (``WHOLE`` ⊂ ``WHOLEFDS``).
        for a in token_set:
            for b in ent.tokens:
                short, long = (a, b) if len(a) <= len(b) else (b, a)
                if len(short) >= _MIN_PREFIX and long.startswith(short):
                    return 0.90

        # Fuzzy: acronym ↔ expansion (``WF`` ↔ ``WHOLE FOODS``), both directions.
        my_acronyms = {t for t in token_set if 2 <= len(t) <= 5} if len(tokens) == 1 else set()
        my_initialism = {_acronym(tokens)} if len(tokens) >= 2 else set()
        if (my_acronyms & ent.initialisms()) or (my_initialism & ent.acronyms()):
            return 0.85

        # Fuzzy: near-identical tokens (spelling drift / typos).
        best = 0.0
        for a in token_set:
            for b in ent.tokens:
                best = max(best, SequenceMatcher(None, a, b).ratio())
        return best if best >= _MIN_CHAR_SIM else 0.0
