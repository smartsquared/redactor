"""Payee category vocabulary + local classification proposer (HSA support).

Why this exists: eligibility questions ("which of these could my HSA pay
for?") are properties of the *counterparty*, and real counterparty names exist
only on the machine holding the mapping table. The answer is therefore computed
locally and only the **verdict** — a category tag — crosses into alias-space.
Entity-level tagging is what alias continuity buys: classify a merchant once
and every transaction resolving to its ``PAYEE-n`` token inherits the tag.

Two privacy invariants this module carries:

  * **The vocabulary is closed.** A category value must be drawn from
    :data:`CATEGORIES` — free text is refused (:func:`validate_category`), so
    the ``payee_category`` field of a projection can never smuggle a real name
    off-machine. The lint enforces the same set on the artifact side.
  * **Classification is a local render surface.** :func:`propose` reads a real
    display label; it runs only where the mapping table lives (the registry's
    review flow), like ``redactor payees --review``. Only the proposed tag —
    closed-vocabulary, non-identifying — ever leaves that surface.

Merchant identity alone cannot settle eligibility for stores that sell both
eligible and ineligible goods; those propose ``mixed-retailer`` and the human
resolves them per-receipt downstream. Which categories a *consumer* treats as
HSA-eligible is finance-domain logic and lives in sakuma-finance, not here —
this module ships the metadata contract only (docs/payee-categories.md).
"""
from __future__ import annotations

# The closed category vocabulary. Deliberately coarse: a tag reveals only the
# *kind* of merchant, never which one. Extend by adding a member here, to the
# lint's mirror, and to docs/payee-categories.md — never by loosening
# validation to arbitrary strings.
CATEGORIES: frozenset[str] = frozenset(
    {
        "pharmacy",           # dedicated pharmacies / drugstores
        "medical-provider",   # clinics, hospitals, physicians, labs, therapy
        "dental",             # dentists, orthodontists
        "vision",             # optometry, ophthalmology, opticians
        "mixed-retailer",     # sells eligible AND ineligible goods; needs receipts
        "non-medical",        # explicitly reviewed and ruled out
    }
)

# The empty string is "untagged" everywhere a category rides along a record; it
# is not a member of the vocabulary and cannot be assigned via tag().
UNTAGGED = ""


def is_valid_category(value: str) -> bool:
    """True iff *value* is a member of the closed vocabulary."""
    return value in CATEGORIES


def validate_category(value: str) -> str:
    """Return *value* if it is a vocabulary member, else raise ``ValueError``.

    The refusal message lists the vocabulary — safe, it contains no real value.
    """
    if not is_valid_category(value):
        allowed = ", ".join(sorted(CATEGORIES))
        raise ValueError(f"unknown category {value!r}; expected one of: {allowed}")
    return value


# -- local classification proposer --------------------------------------------
#
# Keyword rules over the *display label* (a real value; local render only).
# First matching rule wins, so the order below is specificity: a "DENTAL
# CLINIC" is dental, not medical-provider. These are generic merchant-kind
# keywords plus a few well-known national chains — rules, not fixtures, so
# naming real brands here is fine (the fixture-only rule governs test data).
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("dental", ("DENTAL", "DENTIST", "ORTHODONT", "ENDODONT", "PERIODONT")),
    ("vision", ("VISION", "OPTOMETR", "OPHTHALMOLOG", "OPTICAL", "OPTICIAN", "EYE CARE")),
    ("pharmacy", ("PHARMACY", "DRUG STORE", "DRUGSTORE", "APOTHECAR",
                  "CVS", "WALGREEN", "RITE AID")),
    ("medical-provider", ("MEDICAL", "CLINIC", "HOSPITAL", "PHYSICIAN", "PEDIATRIC",
                          "DERMATOLOG", "RADIOLOG", "URGENT CARE", "LABORATOR",
                          "THERAPY", "THERAPIST", "CHIROPRACT", "HEALTHCARE",
                          "HEALTH CENTER", "HEALTH CTR")),
    ("mixed-retailer", ("WALMART", "TARGET", "COSTCO", "AMAZON")),
)


def propose(display_label: str) -> str | None:
    """Propose a category for a payee's real display label, or ``None``.

    Local render surface: the argument is a revealed real value, so this runs
    only on the machine holding the mapping table. Returns a vocabulary member
    on a keyword match; ``None`` means "no opinion" — absence of a proposal is
    not a ``non-medical`` verdict (that tag is an explicit human decision).
    """
    haystack = " ".join(display_label.split()).upper()
    if not haystack:
        return None
    for category, needles in _RULES:
        if any(needle in haystack for needle in needles):
            return category
    return None
