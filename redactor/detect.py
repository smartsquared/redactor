"""Detection: our own ``Detector`` interface wrapping Microsoft Presidio.

Story S1.1. The brief's build-vs-buy decision is **wrap Presidio, do not build
NER**; this module honours that by using Presidio's recognizer machinery while
never letting a Presidio type reach a caller. Callers see only
:class:`DetectedEntity` (our frozen dataclass) and the :class:`Detector`
protocol. Presidio's ``RecognizerResult`` / ``PatternRecognizer`` stay behind
this boundary.

Why we invoke recognizers directly rather than the full ``AnalyzerEngine``:
the finance-tuned recognizers here are pattern- and deny-list-based, which
Presidio can evaluate without an NLP engine (``nlp_artifacts=None``). That keeps
CI light — no spaCy language-model download — while still buying, not building,
the matching. General NER over free prose, if ever needed, slots in behind the
same interface as another recognizer-backed detector.

Detection is upstream of aliasing (S1.2) and entity resolution (S1.3): its job
is to *locate* identifying spans, not to bind them to aliases. The mapping table
is never touched here.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from presidio_analyzer import EntityRecognizer, Pattern, PatternRecognizer

# Our own entity labels. Deliberately independent of Presidio's built-in entity
# names so the wrapped backend can change without shifting the contract callers
# see.
ROUTING_NUMBER = "ROUTING_NUMBER"
ACCOUNT_ID = "ACCOUNT_ID"
INSTITUTION = "INSTITUTION"
PAYEE = "PAYEE"


@dataclass(frozen=True)
class DetectedEntity:
    """One detected identifying span. The public detection type.

    This is intentionally a plain dataclass, not a re-export of Presidio's
    ``RecognizerResult`` — the interface hides Presidio types from callers
    (S1.1 accept). ``text`` is the exact substring that was flagged.
    """

    entity_type: str          # one of the labels above (our namespace)
    start: int                # start offset into the scanned text
    end: int                  # end offset (exclusive)
    text: str                 # the flagged substring, text[start:end]
    score: float              # detector confidence in [0, 1]


@runtime_checkable
class Detector(Protocol):
    """Anything that can locate identifying spans in a string.

    The one method callers depend on. Implementations may wrap Presidio, a
    future NER model, or plain patterns — the return type is always ours.
    """

    def detect(self, text: str) -> list[DetectedEntity]:
        ...


# --- finance-tuned recognizers (Presidio PatternRecognizers under the hood) ---

# Fixture-style account ids: NOWHERE-CHK-000199, PLACEHOLDER-CC-000042. An
# uppercase institution stub, a short account-type code, then a zero-padded
# number — word-bounded so it never swallows surrounding tokens.
_ACCOUNT_ID_REGEX = r"\b[A-Z]{3,}-[A-Z]{2,4}-\d{4,}\b"

# ABA routing / generic 9-digit run, word-bounded on both sides. The word
# boundary keeps it out of amounts (3120.44), ISO dates (2026-04-01) and OFX
# FITIDs (CHK20260401000, where letters glue to the digits with no boundary).
_ROUTING_REGEX = r"\b\d{9}\b"


def _account_id_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity=ACCOUNT_ID,
        patterns=[Pattern(name="fixture_account_id", regex=_ACCOUNT_ID_REGEX, score=0.85)],
    )


def _routing_recognizer() -> PatternRecognizer:
    return PatternRecognizer(
        supported_entity=ROUTING_NUMBER,
        patterns=[Pattern(name="aba_routing_9", regex=_ROUTING_REGEX, score=0.4)],
    )


def _deny_list_recognizer(entity_type: str, terms: Iterable[str]) -> PatternRecognizer:
    """A deny-list recognizer for a set of known entity strings.

    Presidio escapes each term and word-bounds it, so punctuated payee memos
    (``AMAZON.COM*MK12QP``, ``TRADER JOE'S #130``) match exactly.

    The deny-list compiles to one regex alternation, and Python's ``re`` is
    first-match, not longest-match, at a given position. So when one term is a
    prefix of another (case-insensitively) — ``NETFLIX.COM`` vs
    ``Netflix.com CA`` — we must list the longer, more specific term first or
    the shorter one wins and truncates the span. Order by descending length,
    then alphabetically for determinism.
    """
    ordered = sorted(set(terms), key=lambda t: (-len(t), t))
    return PatternRecognizer(supported_entity=entity_type, deny_list=ordered)


class PresidioDetector:
    """A :class:`Detector` backed by a set of Presidio recognizers.

    Holds recognizers privately and runs each one directly (no NLP engine),
    converting Presidio ``RecognizerResult`` objects into :class:`DetectedEntity`
    before anything leaves the method. Overlapping/duplicate spans are
    de-duplicated; results are returned sorted by position.
    """

    def __init__(self, recognizers: Sequence[EntityRecognizer]):
        self._recognizers: tuple[EntityRecognizer, ...] = tuple(recognizers)

    def detect(self, text: str) -> list[DetectedEntity]:
        seen: dict[tuple[str, int, int], DetectedEntity] = {}
        for recognizer in self._recognizers:
            results = recognizer.analyze(
                text, entities=recognizer.supported_entities, nlp_artifacts=None
            )
            for r in results:
                key = (r.entity_type, r.start, r.end)
                candidate = DetectedEntity(
                    entity_type=r.entity_type,
                    start=r.start,
                    end=r.end,
                    text=text[r.start : r.end],
                    score=float(r.score),
                )
                # Keep the higher-confidence hit if two recognizers agree on a span.
                existing = seen.get(key)
                if existing is None or candidate.score > existing.score:
                    seen[key] = candidate
        return sorted(seen.values(), key=lambda e: (e.start, e.end, e.entity_type))


class _FinanceDetectorFactory:
    """Callable factory for the finance detector, with a manifest shortcut.

    ``finance_detector()`` builds the account/routing recognizers plus optional
    institution/payee deny-lists. ``finance_detector.from_manifest(m)`` seeds the
    deny-lists from a fixture manifest's declared entities.
    """

    def __call__(
        self,
        *,
        institutions: Iterable[str] = (),
        payees: Iterable[str] = (),
    ) -> PresidioDetector:
        recognizers: list[EntityRecognizer] = [
            _account_id_recognizer(),
            _routing_recognizer(),
        ]
        institutions = list(institutions)
        payees = list(payees)
        if institutions:
            recognizers.append(_deny_list_recognizer(INSTITUTION, institutions))
        if payees:
            recognizers.append(_deny_list_recognizer(PAYEE, payees))
        return PresidioDetector(recognizers)

    def from_manifest(self, manifest: dict) -> PresidioDetector:
        institutions = {a["institution"] for a in manifest["accounts"].values()}
        payees = {v for variants in manifest["payees"].values() for v in variants}
        return self(institutions=institutions, payees=payees)


finance_detector = _FinanceDetectorFactory()
