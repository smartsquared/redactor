"""Fake-ness lint: fail the build if anything *real-looking* is committed.

Rule 1 of this repo (CLAUDE.md): **no real data, ever.** Fixtures must be
provably fake. This lint is the CI teeth behind that rule. It flags a string as
real-looking when it is:

* an ABA-checksum-**valid** 9-digit routing number,
* a Luhn-**valid** 13–19 digit card / account number, or
* an SSN-shaped ``\\d{3}-\\d{2}-\\d{4}`` string.

Fake identifiers dodge all three on purpose (e.g. routing ``123456789`` fails
the ABA checksum; account ids are alphanumeric). Run as a module in CI:

    python -m redactor.fakeness            # scan default targets, exit 1 on hit
    python -m redactor.fakeness path ...    # scan given files/dirs
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Where real-looking numbers must never appear. Fixtures and docs are the
# fake-only surfaces; source/tests may legitimately carry valid checksums as
# lint test data, so they are not scanned by default.
DEFAULT_TARGETS: tuple[Path, ...] = (
    REPO_ROOT / "tests" / "fixtures",
    REPO_ROOT / "docs",
)

_SCANNABLE_SUFFIXES = {".csv", ".ofx", ".qfx", ".md", ".json", ".txt", ".tsv"}

_DIGIT_RUN = re.compile(r"\d+")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


@dataclass(frozen=True)
class Finding:
    kind: str          # "aba_routing" | "luhn_card" | "ssn"
    match: str         # the offending substring
    path: str          # file the hit was found in ("<text>" for scan_text)
    line: int          # 1-based line number
    col: int           # 1-based column


def _aba_valid(digits: str) -> bool:
    """True if a 9-digit string satisfies the ABA routing checksum."""
    if len(digits) != 9:
        return False
    d = [int(c) for c in digits]
    checksum = (
        3 * (d[0] + d[3] + d[6])
        + 7 * (d[1] + d[4] + d[7])
        + 1 * (d[2] + d[5] + d[8])
    )
    return checksum % 10 == 0


def _luhn_valid(digits: str) -> bool:
    """True if a digit string satisfies the Luhn checksum."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scan_text(text: str, path: str = "<text>") -> list[Finding]:
    """Return every real-looking-identifier finding in *text*."""
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines() or [text], start=1):
        for m in _SSN.finditer(line):
            findings.append(Finding("ssn", m.group(0), path, lineno, m.start() + 1))
        for m in _DIGIT_RUN.finditer(line):
            run = m.group(0)
            col = m.start() + 1
            if len(run) == 9 and _aba_valid(run):
                findings.append(Finding("aba_routing", run, path, lineno, col))
            elif 13 <= len(run) <= 19 and _luhn_valid(run):
                findings.append(Finding("luhn_card", run, path, lineno, col))
    return findings


def _iter_files(target: Path):
    if target.is_dir():
        for p in sorted(target.rglob("*")):
            if p.is_file() and p.suffix in _SCANNABLE_SUFFIXES:
                yield p
    elif target.is_file():
        yield target


def scan_paths(targets: list[Path] | None = None) -> list[Finding]:
    """Scan files under *targets* (default: bundled fixtures + docs)."""
    targets = list(targets) if targets is not None else list(DEFAULT_TARGETS)
    findings: list[Finding] = []
    for target in targets:
        for path in _iter_files(target):
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable: not a fake-ness concern
            rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
            findings.extend(scan_text(text, str(rel)))
    return findings


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    targets = [Path(a) for a in argv] if argv else None
    findings = scan_paths(targets)
    if findings:
        print("fake-ness lint FAILED — real-looking identifiers found:", file=sys.stderr)
        for f in findings:
            print(f"  {f.path}:{f.line}:{f.col}: {f.kind}: {f.match}", file=sys.stderr)
        return 1
    print("fake-ness lint OK — no real-looking identifiers.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
