"""Leak-lint: fail if a seeded identifier appears in an alias-space projection.

Story S1.4 acceptance: "alias-space projection contains zero seeded identifiers."
This module is the teeth. Given the serialized projection text and the set of
identifiers seeded into the fixtures, it flags any that survived into
alias-space — a real raw memo, an account id, an institution name, a routing
number, a display mask — plus any *real-looking* number the fake-ness checks
catch (defense in depth, reusing :mod:`redactor.fakeness`).

A correctly-aliased projection carries only ``CLASS-n`` tokens, amounts and
dates, so this returns nothing. It deliberately touches no mapping-table state:
it scans text, and knows only what a raw statement seeded.
"""
from __future__ import annotations

from dataclasses import dataclass

from redactor.fakeness import scan_text


@dataclass(frozen=True)
class Leak:
    kind: str      # "seed" | fake-ness kind ("aba_routing" | "luhn_card" | "ssn")
    match: str     # the offending identifier / substring
    detail: str = ""  # how it was seeded (e.g. the manifest key)


def seeds_from_manifest(manifest: dict) -> dict[str, str]:
    """Collect every seeded identifier from a fixtures manifest.

    Returns a mapping ``identifier -> where it came from`` (for readable
    findings). Covers account metadata (id, institution, routing, mask) and the
    payee ground truth (canonical names *and* every raw memo variant) — all of
    which are real identifiers the projection must never carry.
    """
    seeds: dict[str, str] = {}

    for acct_type, acct in manifest.get("accounts", {}).items():
        for field in ("account_id", "institution", "routing_number", "display_mask"):
            value = acct.get(field)
            if value:
                seeds[str(value)] = f"accounts.{acct_type}.{field}"

    for canonical, variants in manifest.get("payees", {}).items():
        seeds[canonical] = f"payees.{canonical} (canonical)"
        for variant in variants:
            seeds[variant] = f"payees.{canonical} (variant)"

    return seeds


def scan_projection(text: str, seeds: dict[str, str]) -> list[Leak]:
    """Return every leak of a seeded identifier (or real-looking number) in *text*.

    Seed matching is case-insensitive substring matching: models and stores
    mangle case, and a lowercased memo is just as much a leak as the original.
    """
    leaks: list[Leak] = []
    haystack = text.lower()
    for identifier, source in seeds.items():
        if identifier and identifier.lower() in haystack:
            leaks.append(Leak("seed", identifier, source))

    for finding in scan_text(text):
        leaks.append(Leak(finding.kind, finding.match, f"{finding.line}:{finding.col}"))

    return leaks
