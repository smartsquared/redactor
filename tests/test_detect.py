"""S1.1 accept: the Presidio detection wrapper.

Two acceptance criteria drive these tests, taken verbatim from issue #4 /
docs/m01-stories.md S1.1:

  1. "every seeded identifier in fixtures detected"
  2. "interface hides Presidio types from callers"

The seeded identifiers are exactly what the fixture manifest declares as real,
identifying strings: account ids, the routing number, institution names, and
every payee memo variant. Detection is Presidio wrapped behind our own
``Detector`` interface (wrap, don't build NER — brief decision).
"""
from __future__ import annotations

from redactor.detect import DetectedEntity, Detector, finance_detector
from redactor.fixtures import load_manifest, load_statements


def _seeded_identifiers(manifest: dict) -> set[str]:
    """Every real-looking identifier the fixtures seed (contract §4)."""
    seeded: set[str] = set()
    for acct in manifest["accounts"].values():
        seeded.add(acct["account_id"])
        seeded.add(acct["institution"])
        if "routing_number" in acct:
            seeded.add(acct["routing_number"])
    for variants in manifest["payees"].values():
        seeded.update(variants)
    return seeded


def _fixture_corpus(manifest: dict) -> list[str]:
    """Text a detector would actually see over the fixtures."""
    texts: list[str] = []
    for s in load_statements():
        for t in s.transactions:
            texts.append(t.description)
    for acct in manifest["accounts"].values():
        texts.append(f"account {acct['account_id']} at {acct['institution']}")
        if "routing_number" in acct:
            texts.append(f"routing {acct['routing_number']}")
    return texts


def _detected_texts(detector: Detector, texts: list[str]) -> set[str]:
    found: set[str] = set()
    for text in texts:
        for entity in detector.detect(text):
            found.add(entity.text)
    return found


def test_every_seeded_identifier_in_fixtures_is_detected():
    manifest = load_manifest()
    detector = finance_detector.from_manifest(manifest)

    seeded = _seeded_identifiers(manifest)
    detected = _detected_texts(detector, _fixture_corpus(manifest))

    missing = sorted(seeded - detected)
    assert not missing, f"seeded identifiers not detected: {missing}"


def test_detect_returns_our_type_never_presidio():
    manifest = load_manifest()
    detector = finance_detector.from_manifest(manifest)

    results = detector.detect("routing 123456789 at Bank of Nowhere")
    assert results, "expected at least one detection"
    for entity in results:
        assert isinstance(entity, DetectedEntity)
        # The crown-jewel boundary: no Presidio type may leak to a caller.
        assert "presidio" not in type(entity).__module__


def test_detected_entity_module_is_ours_and_carries_no_presidio_attrs():
    # DetectedEntity is our own dataclass, defined in redactor.detect.
    assert DetectedEntity.__module__ == "redactor.detect"
    entity = DetectedEntity(
        entity_type="ROUTING_NUMBER", start=0, end=9, text="123456789", score=0.4
    )
    for field in ("entity_type", "start", "end", "text", "score"):
        assert hasattr(entity, field)


def test_account_and_routing_patterns_detected_without_a_payee_list():
    # The finance-tuned account/routing recognizers are generic patterns and
    # work with no manifest / deny-list at all.
    detector = finance_detector()

    acct = detector.detect("posted to NOWHERE-CHK-000199 today")
    assert any(e.text == "NOWHERE-CHK-000199" for e in acct), acct

    routing = detector.detect("BANKID 123456789")
    assert any(e.text == "123456789" for e in routing), routing


def test_patterns_do_not_false_fire_on_amounts_dates_or_fitids():
    detector = finance_detector()
    # Amounts, ISO dates, and OFX FITIDs are not identifiers — the 9-digit
    # routing pattern must be word-bounded so it never fires inside them.
    for benign in ("amount -3120.44", "posted 2026-04-01", "FITID CHK20260401000"):
        assert detector.detect(benign) == [], f"false positive on {benign!r}"
