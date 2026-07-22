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
from redactor.resolve import _DEGRADED_VARIANT_CAP, PayeeResolver, normalize

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


# A fixed statement memo tail — the boilerplate a real bank bolts onto *every*
# transaction ("Category Code Withdrawal Debit Card Visa Debit Card" in the
# human's first real ``payees --review``). None of these words are in the
# resolver's stopword list, so they survive :func:`normalize` and, before the
# fix, get title-cased into every display label. They recur on every memo, so
# document frequency identifies them as generic (the #37 lock data).
_BOILERPLATE_TAIL = "CATEGORY CODE WITHDRAWAL DEBIT CARD VISA DEBIT CARD"


def _boilerplate_memos(n_brands: int) -> tuple[list[str], dict[str, str]]:
    """Memos where every brand carries the same fixed boilerplate tail.

    Each brand still varies its city per shape (distinct variants that must
    group onto one entity), but the tail is identical everywhere — the shape
    that title-cased boilerplate into every label."""
    memos: list[str] = []
    truth: dict[str, str] = {}
    for i, brand in enumerate(_BRANDS[:n_brands]):
        for shape in range(5):
            city = _CITIES[(i + shape) % len(_CITIES)]
            state = _STATES[i % len(_STATES)]
            memo = f"POS DEBIT VISA {1000 + i + shape} {brand} {city} {state} {_BOILERPLATE_TAIL}"
            memos.append(memo)
            truth[memo] = brand
    return memos, truth


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


def _degraded_chain_codes(n: int) -> list[str]:
    """*n* distinct discriminative tokens, each a single normalized token.

    Chaining ``codes[i-1] codes[i]`` through the resolver shares exactly one
    token per step, so every memo merges and the entity's token set grows
    without bound — the snowball signature, in miniature."""
    codes = [f"{a}{b}{c}{d}" for a, b, c, d in product("WXYZ", repeat=4)]
    return [c for c in codes if normalize(c) == [c]][:n]


def test_merge_into_large_token_entity_is_flagged_not_locked():
    """An entity that has accumulated a large token set must never absorb a new
    variant at confidence 1.0 — the degraded merge has to be flagged for review.

    The chain here stays *under* the degraded-merge cap (issue #39) so every memo
    still joins the one entity; it pins only the #37 flagging guarantee."""
    resolver = PayeeResolver()
    codes = _degraded_chain_codes(_DEGRADED_VARIANT_CAP + 8)

    confidences: list[float] = []
    resolver.resolve(codes[0])
    for i in range(1, len(codes)):
        _eid, conf, created = resolver.resolve_scored(f"{codes[i - 1]} {codes[i]}")
        confidences.append(conf)
        assert not created, "chain memo (under the cap) should have joined the entity"

    assert resolver.entity_count() == 1, "the sub-cap chain should be one entity"
    # Early joins into a still-small entity are clean 1.0 locks…
    assert confidences[0] == 1.0
    # …but once the entity's token set is large, later joins must be flagged.
    assert confidences[-1] < REVIEW_THRESHOLD, (
        f"merge into a large-token entity scored {confidences[-1]} — not flagged"
    )


# --------------------------------------------------------------------------- #
# Acceptance bar 3 (issue #39): degraded merges are *capped*. An entity that has
# absorbed too many sub-lock variants stops being a merge candidate; further
# borderline memos mint fresh entities rather than snowballing to 113 variants.
# --------------------------------------------------------------------------- #
def test_degraded_merges_are_capped_and_mint_fresh_entities():
    resolver = PayeeResolver()
    codes = _degraded_chain_codes(120)
    assert len(codes) >= 120, "need a long chain to exceed the cap"

    resolver.resolve(codes[0])
    sublock_by_entity: Counter[int] = Counter()
    created_after_cap = 0
    for i in range(1, len(codes)):
        eid, conf, created = resolver.resolve_scored(f"{codes[i - 1]} {codes[i]}")
        if created:
            created_after_cap += 1
        elif conf < 1.0:
            sublock_by_entity[eid] += 1

    # The whole chain did NOT collapse into a single mega-entity…
    assert resolver.entity_count() > 1, "chain still snowballed into one entity"
    assert created_after_cap > 0, "no fresh entities minted after the cap was hit"
    # …and no single entity accrued more than the cap in sub-lock variants.
    worst = max(sublock_by_entity.values())
    assert worst <= _DEGRADED_VARIANT_CAP, (
        f"one entity accrued {worst} sub-lock variants — cap ({_DEGRADED_VARIANT_CAP}) breached"
    )


# --------------------------------------------------------------------------- #
# Acceptance bar 4 (issue #39): display labels carry brand tokens only, never
# the statement's fixed boilerplate tail; pure-boilerplate memos fall back to a
# non-boilerplate label.
# --------------------------------------------------------------------------- #
def _boilerplate_label_tokens() -> set[str]:
    """Title-cased forms of every boilerplate token, as they'd appear in a label."""
    return {tok.capitalize() for tok in normalize(_BOILERPLATE_TAIL)}


def test_display_labels_drop_boilerplate_tail_keeping_brand_only():
    memos, truth = _boilerplate_memos(120)
    resolver, assign = _resolve_batch(memos)
    boilerplate = _boilerplate_label_tokens()

    for memo, eid in assign.items():
        label = resolver.display_name(eid)
        label_tokens = set(label.split())
        assert not (label_tokens & boilerplate), (
            f"label {label!r} still carries statement boilerplate"
        )
        # The brand anchor survives — the label is exactly the brand token.
        assert label == truth[memo].capitalize(), (
            f"label {label!r} is not the bare brand token {truth[memo]!r}"
        )


def test_pure_boilerplate_memo_gets_non_boilerplate_fallback_label():
    memos, _truth = _boilerplate_memos(120)
    resolver, _assign = _resolve_batch(memos)
    boilerplate = _boilerplate_label_tokens()

    # A memo that is *only* boilerplate — no brand token at all.
    label = resolver.resolve_display(_BOILERPLATE_TAIL)
    assert label, "pure-boilerplate memo produced an empty label"
    assert any(c.isalpha() for c in label), "fallback label must be readable"
    assert not (set(label.split()) & boilerplate), (
        f"pure-boilerplate memo produced a boilerplate label: {label!r}"
    )
