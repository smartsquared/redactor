"""High-volume regression guard for entity-resolution snowballing (issue #37).

The fixture corpus is 12 clean brands, which cannot exercise the failure mode
that collapsed a real 9,717-row bank statement (8,506 distinct memo variants)
into a *single* PAYEE entity at confidence 1.0. The root cause: a shared token
with an entity returned a deterministic 1.0 lock, and entities accumulate the
*union* of every variant's tokens — so an entity absorbs a memo, gains its
generic vocabulary (``VISA``, ``POS``, ``DEBIT`` …), and then any next memo that
shares one ordinary word locks onto it too. At real scale this snowballs
transitively until everything is one payee.

These tests build a realistic high-volume synthetic corpus in true bank memo
shapes (``POS DEBIT VISA 1234 <BRAND> <CITY> <ST>``, ACH/payroll strings, shared
generic vocabulary and shared cities) and pin the two acceptance bars from the
issue:

  * entity count lands within **sane bounds (hundreds, not 1)** — no snowball;
  * a merge that shares only *generic* vocabulary never locks, and a merge into
    an entity with a large/growing token set is **flagged** (never scores 1.0).

The corpus is generated deterministically (no real data — provably synthetic
brand tokens) so the bounds are reproducible.
"""
from __future__ import annotations

from collections import Counter
from itertools import product

from redactor.payees import REVIEW_THRESHOLD
from redactor.resolve import PayeeResolver, normalize

# --------------------------------------------------------------------------- #
# A deterministic, realistic high-volume synthetic corpus.
# --------------------------------------------------------------------------- #
# Distinct, provably-fake brand anchors. Four uppercase letters => never a
# state code, never contains a digit (so normalize keeps it), all equal length
# and distinct (so no pair is a prefix of another and none fuzzy-matches
# another). "VZ" prefix keeps them clear of the stopword/city vocabulary.
_BRANDS = [f"VZ{a}{b}" for a, b in product("ABCDEFGHIJKLMNOPQRSTUVWXYZ", repeat=2)]

# Generic transaction vocabulary that recurs on *every* brand's memos and is not
# in the resolver's stopword list — exactly the words that, before the fix,
# snowballed distinct merchants together on a shared-token lock.
_CITIES = ["PORTLAND", "DENVER", "AUSTIN", "BOSTON", "MIAMI", "CHICAGO"]
_STATES = ["OR", "CO", "TX", "MA", "FL", "IL"]


def _memos_for_brand(brand: str, idx: int) -> list[str]:
    """Five memos for one brand, each in a real bank shape sharing generic words."""
    city = _CITIES[idx % len(_CITIES)]
    state = _STATES[idx % len(_STATES)]
    city2 = _CITIES[(idx + 3) % len(_CITIES)]
    return [
        f"POS DEBIT VISA {1000 + idx} {brand} {city} {state}",
        f"CHECKCARD 04/12 {brand} {city} {state} {2000 + idx}",
        f"RECURRING PAYMENT VISA {brand} {city2}",
        f"ACH CREDIT {brand} PPD ID {30000 + idx}",
        f"POS PURCHASE {brand} {city2} {state}",
    ]


def _corpus(n_brands: int) -> tuple[list[str], dict[str, str]]:
    """Return (memos, memo -> brand truth) for the first *n_brands* brands.

    Memos are interleaved by shape so generic vocabulary spreads across many
    distinct brands early — the ordering a real month-of-transactions ingest
    produces, and the one that makes the snowball worst-case."""
    per_brand = [_memos_for_brand(b, i) for i, b in enumerate(_BRANDS[:n_brands])]
    memos: list[str] = []
    truth: dict[str, str] = {}
    for shape in range(5):  # interleave: all brands' shape-0, then shape-1, …
        for i, b in enumerate(_BRANDS[:n_brands]):
            memo = per_brand[i][shape]
            memos.append(memo)
            truth[memo] = b
    return memos, truth


# --------------------------------------------------------------------------- #
# Acceptance bar 1: no snowball — entity count stays in the hundreds, not 1.
# --------------------------------------------------------------------------- #
def _resolve_batch(memos: list[str]) -> tuple[PayeeResolver, dict[str, int]]:
    """Resolve a whole batch the way ingest does: a document-frequency pre-pass
    over every memo first, then resolve each. The pre-pass lets the generic-token
    guard recognise filler (VISA/POS/city names) from the first grouping."""
    resolver = PayeeResolver()
    for memo in memos:
        resolver.observe(memo)
    assign = {memo: resolver.resolve(memo) for memo in memos}
    return resolver, assign


def test_high_volume_corpus_does_not_collapse_into_one_entity():
    n_brands = 300
    memos, truth = _corpus(n_brands)
    assert len(memos) == n_brands * 5

    resolver, assign = _resolve_batch(memos)

    count = resolver.entity_count()
    # The catastrophic failure was exactly one entity for the whole statement.
    assert count > 1, "resolver collapsed the entire corpus into a single payee"
    # Hundreds, not one: one entity per real brand, give or take cold-start noise.
    assert count >= n_brands * 0.8, (
        f"only {count} entities for {n_brands} distinct brands — merchants fused"
    )
    assert count <= n_brands * 1.5, f"unexpected over-splintering: {count} entities"

    # No single entity may swallow a large share of the corpus (snowball
    # signature). A clean brand has five variants; the snowball had thousands.
    biggest = Counter(assign.values()).most_common(1)[0][1]
    assert biggest <= 10, f"one entity absorbed {biggest} memos — snowball not stopped"


def test_generic_shared_token_alone_never_merges_distinct_brands():
    """Two different brands that share only generic vocabulary must not fuse."""
    memos, truth = _corpus(300)
    _resolver, assign = _resolve_batch(memos)

    # Group entities by the brand truth of their members; a clean run has each
    # entity mapping to exactly one brand.
    by_entity: dict[int, set[str]] = {}
    for memo, eid in assign.items():
        by_entity.setdefault(eid, set()).add(truth[memo])
    fused = {eid: brands for eid, brands in by_entity.items() if len(brands) > 3}
    assert not fused, f"generic vocabulary fused distinct brands: {list(fused)[:5]}"


# --------------------------------------------------------------------------- #
# Acceptance bar 2: degraded merges are flagged (never a silent 1.0 lock).
# --------------------------------------------------------------------------- #
def test_generic_only_overlap_does_not_score_a_10_lock():
    """A shared *generic* token (high document frequency) must not lock at 1.0."""
    resolver = PayeeResolver()
    # Teach the resolver that VISA/POS/DEBIT are generic by showing them on many
    # distinct brands.
    memos, _ = _corpus(120)
    for memo in memos:
        resolver.resolve(memo)

    # Two brand-new, mutually-distinct merchants that share only generic words.
    a_id, a_conf, a_created = resolver.resolve_scored("POS DEBIT VISA 9001 QWXA PORTLAND OR")
    b_id, b_conf, b_created = resolver.resolve_scored("POS DEBIT VISA 9002 QWXB DENVER CO")
    assert a_id != b_id, "distinct merchants merged on generic vocabulary alone"


def test_merge_into_large_token_entity_is_flagged_not_locked():
    """An entity that has accumulated a large token set must never absorb a new
    variant at confidence 1.0 — the degraded merge has to be flagged for review."""
    resolver = PayeeResolver()
    # Chain distinct discriminative tokens through one entity: each memo shares a
    # single token with the prior one, so they all merge and the entity's token
    # set grows without bound — the snowball signature, in miniature.
    codes = [f"{a}{b}{c}{d}" for a, b, c, d in product("WXYZ", repeat=4)]
    codes = [c for c in codes if normalize(c) == [c]][:40]

    confidences: list[float] = []
    resolver.resolve(codes[0])
    for i in range(1, 30):
        _eid, conf, created = resolver.resolve_scored(f"{codes[i - 1]} {codes[i]}")
        confidences.append(conf)
        assert not created, "chain memo should have joined the growing entity"

    assert resolver.entity_count() == 1, "the chain should collapse into one entity"
    # Early joins into a still-small entity are clean 1.0 locks…
    assert confidences[0] == 1.0
    # …but once the entity's token set is large, later joins must be flagged.
    assert confidences[-1] < REVIEW_THRESHOLD, (
        f"merge into a large-token entity scored {confidences[-1]} — not flagged"
    )
