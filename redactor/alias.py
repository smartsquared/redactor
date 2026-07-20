"""Reference implementation of the alias-space token grammar & matching rule.

This is the executable companion to ``docs/alias-contract.md``. It fixes the
canonical token form and the mangle-tolerant, never-false-positive inbound
matcher so later stories (S2.1 outbound, S2.2 inbound lens) build on one
definition. It does **not** touch the mapping table — it only recognises token
*shapes*; binding tokens to real values is the crown-jewel path and lives
elsewhere, local-only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# The closed set of entity classes for m01 (contract §1.1). Adding one is a
# contract change.
CLASSES: tuple[str, ...] = ("PAYEE", "ACCT", "INST", "PERSON", "CARD")

# Contract §3.3. Read case-insensitively on the class.
#   (?<![A-Za-z0-9]) — class-word boundary: no letter/digit glued in front
#   [-_ ]            — canonical hyphen, or the manglings underscore / space
#   (\d+)            — the number, matched whole
#   (?:['’]?s|['’]s)? — optional possessive / plural suffix
#   (?![0-9])        — whole-number boundary: no trailing digit
ALIAS_RE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(CLASSES) + r")[-_ ](\d+)(?:['’]?s|['’]s)?(?![0-9])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AliasMatch:
    """One recognised alias token in a scanned string."""

    cls: str          # canonical uppercase class, e.g. "PAYEE"
    n: int            # the alias number
    span: tuple[int, int]  # (start, end) offsets of the raw match in the text
    raw: str          # the exact substring matched, manglings and all

    @property
    def canonical(self) -> str:
        return canonical(self.cls, self.n)


def canonical(cls: str, n: int) -> str:
    """Return the canonical token string for a class + number.

    Raises ValueError if the class is outside the closed set or n is not a
    positive integer (contract §1, §2.3).
    """
    up = cls.upper()
    if up not in CLASSES:
        raise ValueError(f"unknown alias class: {cls!r}")
    if not isinstance(n, int) or n < 1:
        raise ValueError(f"alias number must be a positive integer, got {n!r}")
    return f"{up}-{n}"


def find_aliases(text: str) -> list[AliasMatch]:
    """Find every alias token in *text*, mangle-tolerantly (contract §3).

    Never fires on non-alias prose: the class must stand alone as a word, the
    number is matched whole, and only the closed class set is recognised.
    """
    out: list[AliasMatch] = []
    for m in ALIAS_RE.finditer(text):
        out.append(
            AliasMatch(
                cls=m.group(1).upper(),
                n=int(m.group(2)),
                span=(m.start(), m.end()),
                raw=m.group(0),
            )
        )
    return out
