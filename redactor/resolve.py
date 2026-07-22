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
   *discriminative* significant token (e.g. ``COSTCO``) resolve together with
   full confidence. Sharing a token that recurs across many distinct memos —
   generic vocabulary the stopword list does not enumerate (``VISA``, ``POS``,
   a big-city name) — is **not** a lock: document frequency, learned online,
   demotes such tokens so they can never snowball distinct merchants into one
   entity at real scale (issue #37).
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

# --- Snowball guards (issue #37) ------------------------------------------- #
# A shared token is only a *discriminative* lock if it is not generic. A token
# is generic once it has been seen on many distinct memos: it is transaction
# filler (``VISA``, ``POS``, ``DEBIT``, a big-city name) that carries no merchant
# identity, so sharing it must never fuse two payees. Genericity needs both an
# absolute floor (so a token seen only a handful of times is never demoted — the
# clean fixtures never trip this) and a corpus fraction (so at real scale the
# ubiquitous words drop out). Document frequency is learned online as memos
# arrive, exactly as the issue's fix direction prescribes.
_GENERIC_MIN_DOCS = 8
_GENERIC_DF_FRACTION = 0.10

# The most *discriminative* tokens an entity may accumulate before a further
# shared-token match is treated as degraded rather than a clean 1.0 lock. A
# healthy payee's identity is one or two brand tokens; an entity whose
# discriminative token set has grown past this is a snowball-in-progress, so a
# new variant joining it is flagged for review (never scores 1.0) instead of
# being silently absorbed at full confidence.
_ANCHOR_TOKEN_CAP = 8

# Confidence assigned to a shared-token match into an over-grown entity: it still
# clears the resolver threshold (the memo joins rather than splintering) but sits
# below the review threshold, so the degraded grouping is surfaced to a human.
_DEGRADED_LOCK = 0.90

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


def display_label(memo: str) -> str:
    """A human-readable display name for the payee behind a raw memo.

    Built from the memo's *significant tokens* — the brand anchor
    :func:`normalize` already isolates from the store-number / city / date noise —
    title-cased so un-redaction reads as a name a human recognises ("Costco",
    "Whole Foods"), not a store number or an internal id. Pure-filler memos with
    no significant token fall back to their whitespace-cleaned raw form.

    This is the label a resolved entity carries into the alias mapping as its
    real value (S1.2): the lens reveals *this*, not the resolver's opaque entity
    id (issue #25). It is a memo-string function only — no real data, no table.
    """
    sig = normalize(memo)
    if sig:
        return " ".join(tok.capitalize() for tok in sig)
    return " ".join(memo.split()) or "Unknown Payee"


@dataclass
class _Entity:
    """One resolved payee: an id plus the accumulated evidence about it."""

    id: int
    display: str = ""  # human-readable label, fixed at creation (issue #25)
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
        # Online document frequency: how many *distinct* memo variants each token
        # has appeared on, plus the running total of distinct variants. Learned
        # from :meth:`resolve` only (never :meth:`match`, which is non-mutating),
        # so the outbound proxy cannot skew what counts as generic. This is what
        # demotes ubiquitous filler (VISA/POS/DEBIT) out of the lock (issue #37).
        self._doc_freq: dict[str, int] = {}
        self._doc_count = 0
        self._seen_docs: set[tuple[str, ...]] = set()
        # id -> display label, fixed at entity creation and kept even after a
        # merge folds the entity away, so a display never silently changes under
        # an already-issued alias. ``_claimed`` guards against two distinct
        # entities landing on the same label, which would false-merge them at the
        # mapping layer (the real value is the mapping key). Both only ever grow.
        self._display_by_id: dict[int, str] = {}
        self._claimed: set[str] = set()

    def entity_count(self) -> int:
        return len(self._entities)

    def _claim_display(self, eid: int, memo: str) -> str:
        """Fix a fresh entity's human-readable label, unique across entities.

        The label is derived from the *creating* memo and then frozen, so every
        later variant of the same payee reuses it (alias stability, §2.4). On the
        rare chance two distinct entities derive the same label, a disambiguator
        is appended — a same-label collision would false-merge them at the mapping
        layer, since the display is the mapping's real value (issue #25).
        """
        base = display_label(memo)
        label = base
        while label in self._claimed:
            label = f"{base} ({eid})"
        self._claimed.add(label)
        self._display_by_id[eid] = label
        return label

    def display_name(self, eid: int) -> str | None:
        """The human-readable display label for an entity id, or ``None``.

        This is what ingest stores as the ``PAYEE`` real value and the lens
        reveals — a merchant name, never the opaque entity id (issue #25)."""
        return self._display_by_id.get(eid)

    def resolve_display(self, memo: str) -> str:
        """Resolve *memo* to its payee entity and return that entity's display
        label. The ingest seam (S1.4): same payee -> same stable, human-readable
        real value in the mapping table."""
        return self._display_by_id[self.resolve(memo)]

    def match_display(self, memo: str) -> str | None:
        """Non-mutating twin of :meth:`resolve_display` for the outbound proxy.

        Returns the display label of the entity *memo* resolves to, or ``None``
        when it matches nothing known — never creating a phantom entity."""
        eid = self.match(memo)
        return None if eid is None else self._display_by_id.get(eid)

    def match(self, memo: str) -> int | None:
        """Return the id of the entity *memo* resolves to, or ``None``.

        The non-mutating twin of :meth:`resolve`: it never creates an entity and
        never absorbs evidence. That is exactly what the outbound proxy (S2.1)
        needs — a user mention that matches nothing known must be *surfaced as
        unresolved*, never allowed to silently spawn a phantom payee that would
        then look "known". Returns the highest-confidence entity at or above the
        threshold, else ``None``.
        """
        tokens = normalize(memo)
        if not tokens:
            tokens = [re.sub(r"[^A-Z]", "", memo.upper()) or "UNKNOWN"]

        best_id: int | None = None
        best_score = 0.0
        for ent in self._entities:
            score = self._confidence(tokens, ent)
            if score >= self._threshold and score > best_score:
                best_score = score
                best_id = ent.id
        return best_id

    def resolve(self, memo: str) -> int:
        """Resolve *memo* to a payee entity id, creating one if none matches."""
        return self.resolve_scored(memo)[0]

    def observe(self, memo: str) -> None:
        """Fold *memo* into the document-frequency counts without resolving it.

        A pure pre-pass seam: it creates no entity and issues no id, it only
        teaches the resolver how widely each token is spread across the corpus.
        Running :meth:`observe` over a whole batch *before* resolving lets the
        generic-token guard (issue #37) recognise ubiquitous filler like
        ``VISA``/``POS`` from the very first :meth:`resolve` call, instead of
        fusing a cold-start handful of merchants before document frequency has
        warmed up. Idempotent per distinct memo, so seeding then resolving the
        same batch double-counts nothing."""
        tokens = normalize(memo)
        if not tokens:
            tokens = [re.sub(r"[^A-Z]", "", memo.upper()) or "UNKNOWN"]
        self._observe(tokens)

    def resolve_scored(self, memo: str) -> tuple[int, float, bool]:
        """Resolve *memo*, also reporting how the decision was made.

        Returns ``(entity_id, confidence, created)``:

          * ``entity_id`` — the stable id, exactly as :meth:`resolve`.
          * ``confidence`` — the score of the winning match when *memo* joined an
            existing entity (``1.0`` for a deterministic shared-token lock, lower
            for a fuzzy prefix/acronym/typo merge). When a fresh entity is
            created there is no grouping decision to doubt, so it is ``1.0``.
          * ``created`` — ``True`` iff a brand-new entity was minted.

        This is the seam the persistent review flow (story S1.1) reads: a fuzzy
        merge (confidence below the review threshold) is what ingest flags for a
        human to confirm."""
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

        # Learn this memo's tokens *after* scoring it against prior evidence, so a
        # memo never dilutes the genericity of its own tokens for its own decision.
        self._observe(tokens)

        if not matches:
            ent = _Entity(id=self._next_id)
            self._next_id += 1
            ent.display = self._claim_display(ent.id, memo)
            ent.absorb(tokens)
            self._entities.append(ent)
            return ent.id, 1.0, True

        # Join the best match, and fold in every other above-threshold entity:
        # a memo that independently matches two existing entities is fresh
        # evidence that they were the same merchant all along (this is what makes
        # resolution robust to the order memos arrive in). Sub-threshold entities
        # are untouched — no silent merges.
        matches.sort(key=lambda m: m[0], reverse=True)
        best_score = matches[0][0]
        target = matches[0][1]
        target.absorb(tokens)
        for _, other in matches[1:]:
            target.tokens.update(other.tokens)
            target.members.extend(other.members)
            self._entities.remove(other)
        return target.id, best_score, False

    # -- document frequency (the generic-token guard) --------------------- #
    def _observe(self, tokens: list[str]) -> None:
        """Fold one memo's tokens into the online document-frequency counts.

        Counted once per *distinct* normalized memo (re-resolving the same memo,
        as happens on re-ingest, never inflates a token's frequency), so the
        counts measure how widely a token is spread across the corpus — the
        signal that separates a merchant's brand token from generic filler."""
        key = tuple(tokens)
        if key in self._seen_docs:
            return
        self._seen_docs.add(key)
        self._doc_count += 1
        for tok in set(tokens):
            self._doc_freq[tok] = self._doc_freq.get(tok, 0) + 1

    def _is_generic(self, token: str) -> bool:
        """Has *token* recurred on enough distinct memos to be non-identifying?

        Generic tokens (transaction filler, ubiquitous city names) carry no
        merchant identity, so sharing one must never lock two payees together
        (issue #37). Requires both an absolute floor and a corpus-fraction bar,
        so a token seen only a handful of times — every token in the small clean
        fixtures — is never demoted."""
        df = self._doc_freq.get(token, 0)
        return df >= _GENERIC_MIN_DOCS and df >= _GENERIC_DF_FRACTION * self._doc_count

    def _discriminative(self, tokens: set[str]) -> set[str]:
        """The subset of *tokens* that still carry merchant identity."""
        return {t for t in tokens if not self._is_generic(t)}

    # -- scoring ---------------------------------------------------------- #
    def _confidence(self, tokens: list[str], ent: _Entity) -> float:
        """Confidence in ``tokens`` and ``ent`` being the same payee, in [0, 1]."""
        token_set = set(tokens)

        # Deterministic: a shared *discriminative* token is a lock. Generic filler
        # (``VISA``, ``POS``, a big-city name) is excluded on both sides, so it can
        # never fuse two merchants — the fix for the snowball (issue #37). A lock
        # into an entity whose discriminative identity has already grown large is
        # a degraded merge: it still joins, but scores below the review threshold
        # so a human is asked to confirm rather than it landing silently at 1.0.
        shared = self._discriminative(token_set) & self._discriminative(ent.tokens)
        if shared:
            if len(self._discriminative(ent.tokens)) > _ANCHOR_TOKEN_CAP:
                return _DEGRADED_LOCK
            return 1.0

        # Fuzzy: a significant token contains another as a long prefix
        # (``WHOLE`` ⊂ ``WHOLEFDS``). Generic filler is excluded here too.
        my_disc = self._discriminative(token_set)
        ent_disc = self._discriminative(ent.tokens)
        for a in my_disc:
            for b in ent_disc:
                short, long = (a, b) if len(a) <= len(b) else (b, a)
                if len(short) >= _MIN_PREFIX and long.startswith(short):
                    return 0.90

        # Fuzzy: acronym ↔ expansion (``WF`` ↔ ``WHOLE FOODS``), both directions.
        my_acronyms = {t for t in my_disc if 2 <= len(t) <= 5} if len(tokens) == 1 else set()
        my_initialism = {_acronym(tokens)} if len(tokens) >= 2 else set()
        if (my_acronyms & ent.initialisms()) or (my_initialism & ent.acronyms()):
            return 0.85

        # Fuzzy: near-identical tokens (spelling drift / typos).
        best = 0.0
        for a in my_disc:
            for b in ent_disc:
                best = max(best, SequenceMatcher(None, a, b).ratio())
        return best if best >= _MIN_CHAR_SIM else 0.0
