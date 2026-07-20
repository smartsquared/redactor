"""Loader for the synthetic bookkeeping fixtures (story S0.2).

Reads the CSV + OFX statement files under ``tests/fixtures/bookkeeping/`` into
plain dataclasses so downstream stories (detection, aliasing, resolution, the
falsifier harness) have one way in. The loader is intentionally dependency-free:
CSV via the stdlib, OFX 1.x SGML via a small tolerant parser (unclosed value
tags and all).

Nothing here reaches real data — the fixtures are provably fake (see
``docs/alias-contract.md`` §4 and the fake-ness lint).
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "bookkeeping"


@dataclass(frozen=True)
class Transaction:
    date: str          # ISO YYYY-MM-DD
    amount: float      # signed; negative = money out
    description: str    # raw memo string, as the bank rendered it
    ttype: str = ""    # OFX TRNTYPE when known (DEBIT/CREDIT)
    fitid: str = ""    # OFX FITID when known
    category: str = ""  # CSV category column when present


@dataclass(frozen=True)
class Statement:
    account_type: str   # "checking" | "credit"
    format: str         # "csv" | "ofx"
    month: str          # "2026-04"
    institution: str
    account_id: str
    source: Path
    transactions: list[Transaction] = field(default_factory=list)


def load_manifest(directory: Path | None = None) -> dict:
    """Return the fixture manifest (payee-variant ground truth, account meta)."""
    directory = directory or FIXTURES_DIR
    with open(directory / "manifest.json", encoding="utf-8") as fh:
        return json.load(fh)


def _iso(ofx_date: str) -> str:
    """OFX date (YYYYMMDD, possibly with a time suffix) -> ISO YYYY-MM-DD."""
    d = ofx_date.strip()[:8]
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


def _parse_csv(path: Path) -> list[Transaction]:
    txns: list[Transaction] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            txns.append(
                Transaction(
                    date=row["Date"].strip(),
                    amount=float(row["Amount"]),
                    description=row["Description"].strip(),
                    category=row.get("Category", "").strip(),
                )
            )
    return txns


_TAG = re.compile(r"^<([A-Z0-9]+)>(.*)$")


def _parse_ofx(path: Path) -> tuple[str, str, list[Transaction]]:
    """Tolerant OFX 1.x SGML parse. Returns (institution, account_id, txns)."""
    institution = ""
    account_id = ""
    txns: list[Transaction] = []
    cur: dict[str, str] | None = None

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        m = _TAG.match(line)
        if line == "<STMTTRN>":
            cur = {}
            continue
        if line == "</STMTTRN>":
            if cur is not None:
                txns.append(
                    Transaction(
                        date=_iso(cur.get("DTPOSTED", "")),
                        amount=float(cur.get("TRNAMT", "0")),
                        description=cur.get("NAME", "").strip(),
                        ttype=cur.get("TRNTYPE", ""),
                        fitid=cur.get("FITID", ""),
                    )
                )
            cur = None
            continue
        if not m:
            continue
        tag, value = m.group(1), m.group(2).strip()
        if cur is not None:
            cur[tag] = value
        elif tag == "ORG" and not institution:
            institution = value
        elif tag == "ACCTID" and not account_id:
            account_id = value
    return institution, account_id, txns


def load_statements(directory: Path | None = None) -> list[Statement]:
    """Load every fixture statement in *directory* (default: bundled fixtures).

    Filenames encode the metadata: ``<account_type>_<month>.<ext>`` e.g.
    ``checking_2026-04.csv``.
    """
    directory = directory or FIXTURES_DIR
    manifest = load_manifest(directory)
    accounts = manifest["accounts"]

    statements: list[Statement] = []
    for path in sorted(directory.glob("*")):
        if path.suffix not in (".csv", ".ofx"):
            continue
        stem = path.stem  # e.g. checking_2026-04
        if "_" not in stem:
            continue
        account_type, month = stem.split("_", 1)
        if account_type not in accounts:
            continue
        fmt = path.suffix.lstrip(".")

        if fmt == "csv":
            txns = _parse_csv(path)
            institution = accounts[account_type]["institution"]
            account_id = accounts[account_type]["account_id"]
        else:
            institution, account_id, txns = _parse_ofx(path)
            # OFX carries account_id; institution ORG may be terse — prefer the
            # manifest's canonical institution name for consistency.
            institution = institution or accounts[account_type]["institution"]

        statements.append(
            Statement(
                account_type=account_type,
                format=fmt,
                month=month,
                institution=institution,
                account_id=account_id,
                source=path,
                transactions=txns,
            )
        )
    return statements
