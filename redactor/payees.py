"""Payee entity-resolution review flow — the persistent registry (story S1.1).

The in-memory :class:`~redactor.resolve.PayeeResolver` is the algorithm; this
module is its **store-backed skin**. It binds resolved payee entities to stable
``PAYEE-n`` aliases in the encrypted mapping table, remembers which memo variant
landed on which entity (with what confidence), and exposes the human-facing
review verbs the human runs on the machine that holds the table:

  * :meth:`PayeeRegistry.record` — ingest seam: resolve a memo, issue/reuse its
    ``PAYEE`` alias, and persist the variant + confidence. Known variants are
    served straight from the store, so re-ingest is stable and cross-session
    grouping never drifts.
  * :meth:`PayeeRegistry.review` — list entities with variant counts, confidence,
    and display labels. This is a **local render** surface (it reveals real
    values), for the human's eyes on the machine holding the mapping table.
  * :meth:`PayeeRegistry.merge` / :meth:`split` / :meth:`split_variant` /
    :meth:`rename` — fix wrong groupings and labels. A merge aliases the loser to
    the winner and **never renumbers** (alias stability, docs/alias-contract.md
    §2.3–2.4); every mutation is journaled in the store (auditable).

Nothing here serializes the mapping table off-store: it reads and writes only
through the encrypted ``store`` connection. Display labels and memo variants are
real values and stay inside the store; only :meth:`review` crosses the render
boundary, deliberately and locally.
"""
from __future__ import annotations

from dataclasses import dataclass

from redactor.alias import find_aliases
from redactor.resolve import PayeeResolver, display_label

# A grouping whose confidence is below this is *flagged for review*: it was
# formed by a fuzzy match (prefix / acronym / typo similarity) rather than a
# deterministic shared-token lock (confidence 1.0). Those are the groupings a
# human should confirm — the ones real memo garbage most often gets wrong.
REVIEW_THRESHOLD = 0.95

# The class this registry manages. Payees are the only resolved-from-garbage
# entity class; accounts and institutions come straight from statement metadata.
_PAYEE = "PAYEE"


def _variant_key(memo: str) -> str:
    """Normalize a memo to the key the variant table dedups on.

    Case- and whitespace-folded so ``"Whole Foods"`` and ``"WHOLE  FOODS"`` count
    as one variant. This is *not* the resolver's normalization (which strips
    store numbers and cities) — it is only the identity of a distinct memo
    string, so ``"WHOLEFDS #1029 SEA"`` and ``"WF MKT 445"`` stay two variants."""
    return " ".join(memo.split()).upper()


@dataclass(frozen=True)
class PayeeEntity:
    """One payee entity, as the review flow presents it (local render).

    ``display`` is a revealed real value — this dataclass is for the human's
    terminal on the machine holding the mapping table, never an off-machine
    artifact."""

    token: str            # the canonical PAYEE-n alias
    display: str          # the human-readable label (real value; local only)
    variant_count: int    # distinct memo strings grouped under it
    confidence: float     # the weakest grouping confidence among its variants
    low_confidence: bool  # True => below REVIEW_THRESHOLD, flagged for review


class PayeeRegistry:
    """Store-backed payee registry: resolution, review, and mutation."""

    def __init__(self, store, *, review_threshold: float = REVIEW_THRESHOLD) -> None:
        self._store = store
        self._review_threshold = review_threshold
        self._resolver = PayeeResolver()
        # Map the resolver's opaque entity id onto the persistent PAYEE token, so
        # a new variant that the resolver groups with a known entity reuses that
        # entity's stable alias. Rebuilt from the store on every open.
        self._eid_to_token: dict[int, str] = {}
        # Groupings flagged low-confidence *during this session's records* — the
        # signal ingest surfaces. Alias-space (token -> confidence).
        self._flagged: dict[str, float] = {}
        self._reseed()

    def _reseed(self) -> None:
        """Rebuild the resolver's grouping from the persisted variants.

        Replaying stored variants in insertion order reconstructs the same
        entity structure a prior session built, and re-links each resolver
        entity id to the PAYEE token the store already bound it to — so a memo
        never seen before still groups (and aliases) consistently with history.
        """
        for variant, token, _confidence in self._store.payee_variants():
            eid = self._resolver.resolve(variant)
            self._eid_to_token[eid] = token

    # -- ingest seam ---------------------------------------------------------

    def observe(self, memo: str) -> None:
        """Pre-seed the resolver's document frequency with *memo* (issue #37).

        A batch caller runs this over every memo it is about to :meth:`record`
        so the generic-token guard recognises ubiquitous filler (``VISA``,
        ``POS``, a big-city name) from the first grouping decision, rather than
        cold-start-fusing a handful of merchants before frequency warms up.
        Creates no entity, issues no alias — a pure statistical pre-pass."""
        self._resolver.observe(memo)

    def record(self, memo: str) -> str:
        """Resolve *memo* to its PAYEE alias, persisting the variant + confidence.

        A memo string already recorded returns its stored token unchanged (this
        is what makes re-ingest issue zero new aliases). A new string is resolved
        by the fuzzy resolver: if it groups with a known entity it reuses that
        entity's token; otherwise a fresh token is issued for its display label.
        The grouping confidence is stored and, when below the review threshold,
        flagged for the human to confirm."""
        key = _variant_key(memo)
        recorded = self._store.payee_variant(key)
        if recorded is not None:
            return recorded[0]

        eid, score, _created = self._resolver.resolve_scored(memo)
        token = self._eid_to_token.get(eid)
        if token is None:
            # A fresh entity: bind its (disambiguated, unique) display label to a
            # stable alias. assign() is idempotent and gap-tolerant — a label
            # already bound (same brand surfacing under a splinter) reuses that
            # token, which is a correct same-payee merge, not a collision.
            display = self._resolver.display_name(eid)
            token = self._store.mapping.assign(_PAYEE, display)
            self._eid_to_token[eid] = token
            confidence = 1.0
        else:
            confidence = score

        self._store.add_payee_variant(key, token, confidence)
        if confidence < self._review_threshold:
            head = self._store.mapping.canonical_head(token) or token
            prev = self._flagged.get(head)
            self._flagged[head] = confidence if prev is None else min(prev, confidence)
        return token

    def flagged(self) -> list[tuple[str, float]]:
        """Low-confidence groupings recorded this session: ``(token, confidence)``.

        Alias-space only — safe for the ingest summary. Sorted by PAYEE number so
        the human sees a stable order."""
        return sorted(self._flagged.items(), key=lambda kv: _token_n(kv[0]))

    # -- review (local render boundary) --------------------------------------

    def review(self) -> list[PayeeEntity]:
        """List the canonical payee entities for human review.

        Reveals display labels — a local render surface, for the machine that
        holds the mapping table only. Merged-away losers are folded into their
        winner; each entity's variant count and confidence aggregate every memo
        string that resolves to it (its own and any merged in)."""
        counts: dict[str, int] = {}
        worst: dict[str, float] = {}
        for _variant, token, confidence in self._store.payee_variants():
            head = self._store.mapping.canonical_head(token) or token
            counts[head] = counts.get(head, 0) + 1
            worst[head] = min(worst.get(head, 1.0), confidence)

        entities: list[PayeeEntity] = []
        for token in self._store.mapping.canonical_tokens(_PAYEE):
            sealed = self._store.mapping.real_value_of(token)
            display = sealed.reveal() if sealed is not None else ""
            confidence = worst.get(token, 1.0)
            entities.append(
                PayeeEntity(
                    token=token,
                    display=display,
                    variant_count=counts.get(token, 0),
                    confidence=confidence,
                    low_confidence=confidence < self._review_threshold,
                )
            )
        entities.sort(key=lambda e: _token_n(e.token))
        return entities

    # -- mutations (all journaled) -------------------------------------------

    def merge(self, winner: str, loser: str) -> None:
        """Fold *loser* into *winner* (loser aliased to winner, never renumbered).

        Both keep their tokens; the loser now resolves to the winner's real value
        and its variants count under the winner. Refuses a no-op or a merge that
        would form a cycle."""
        self._require_payee(winner)
        self._require_payee(loser)
        w_head = self._store.mapping.canonical_head(winner)
        l_head = self._store.mapping.canonical_head(loser)
        if w_head == l_head:
            raise ValueError(f"{winner} and {loser} are already the same entity")
        self._store.mapping.set_merged_into(loser, w_head)
        self._store.add_payee_journal("merge", winner=w_head, loser=loser)

    def split(self, token: str) -> None:
        """Un-merge *token* — the inverse of :meth:`merge`.

        *token* becomes its own entity again, resolving to its own real value and
        reclaiming the variants that were tagged to it."""
        self._require_payee(token)
        if self._store.mapping.merged_into(token) is None:
            raise ValueError(f"{token} is not merged into anything; nothing to split")
        self._store.mapping.clear_merged_into(token)
        self._store.add_payee_journal("split", loser=token)

    def split_variant(self, token: str, memo: str) -> str:
        """Pull a single memo *variant* off *token* into a brand-new entity.

        The finer-grained split: for when one entity actually holds two
        merchants. Mints a fresh, never-reused token (alias stability) and moves
        the variant onto it. Returns the new token."""
        self._require_payee(token)
        key = _variant_key(memo)
        recorded = self._store.payee_variant(key)
        if recorded is None:
            raise ValueError(f"no recorded variant for {memo!r}")
        cur_head = self._store.mapping.canonical_head(recorded[0])
        if cur_head != self._store.mapping.canonical_head(token):
            raise ValueError(f"{memo!r} does not belong to {token}")
        new_token = self._mint_new_token(display_label(memo))
        self._store.reassign_payee_variant(key, new_token)
        self._store.add_payee_journal("split", winner=token, loser=new_token)
        return new_token

    def rename(self, token: str, label: str) -> None:
        """Change an entity's display label (real value). Token unchanged.

        Refused (``ValueError``) if the label already names another entity — that
        is a merge, not a rename."""
        self._require_payee(token)
        if not label.strip():
            raise ValueError("a display label must not be empty")
        self._store.mapping.rename_value(token, label)
        self._store.add_payee_journal("rename", winner=token)

    def journal(self) -> list[dict]:
        """The auditable payee-mutation history (oldest first)."""
        return self._store.payee_journal()

    # -- internals -----------------------------------------------------------

    def _mint_new_token(self, display: str) -> str:
        """Issue a guaranteed-fresh PAYEE token, disambiguating a label clash.

        assign() would return an *existing* token if the label is already bound;
        a split must always create a new entity, so a colliding label is suffixed
        until it is unique before issuance."""
        label = display
        n = 2
        while self._store.mapping.resolve_token(_PAYEE, label) is not None:
            label = f"{display} ({n})"
            n += 1
        return self._store.mapping.assign(_PAYEE, label)

    def _require_payee(self, token: str) -> None:
        """Validate *token* is a known PAYEE alias, else raise ``ValueError``."""
        matches = find_aliases(token)
        if len(matches) != 1 or matches[0].canonical != token or matches[0].cls != _PAYEE:
            raise ValueError(f"not a canonical PAYEE token: {token!r}")
        if self._store.mapping.real_value_of(token) is None:
            raise ValueError(f"unknown PAYEE token: {token!r}")


def _token_n(token: str) -> int:
    """The numeric part of a PAYEE-n token, for stable ordering."""
    matches = find_aliases(token)
    return matches[0].n if matches else 0
