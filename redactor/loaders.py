"""Tolerant loaders for real bank exports (story S0.3).

The bundled fixtures in :mod:`redactor.fixtures` are clean and uniform; real
exports are not. Banks disagree on header names, ship files with a UTF-8 BOM or
a Windows codepage, render dates as ``MM/DD/YYYY``, split money into separate
debit/credit columns, and emit OFX in either the 1.x SGML dialect or the 2.x XML
dialect. This module hardens the CSV and OFX parsers so a real month of
statements ingests — or fails with an **actionable, value-free error**.

Two design rules carry the repo's crown-jewel discipline into error handling:

  * **Errors never echo raw field values.** A parse failure reports the *row
    number* and the *column name* (both safe: a header label and an ordinal are
    not sensitive), but never the offending cell — that could be an account
    number a user then copy-pastes into a chat. The raw value is available only
    behind the explicit :meth:`LoadError.render(show_raw=True)` gate, mirroring
    :class:`redactor.store.Sealed`. ``str(err)`` is value-free too.
  * **Detection stays structural.** The loader resolves columns and normalizes
    values; it does not itself redact. Redaction is the ingest adapter's job
    (:mod:`redactor.ingest`), which replaces the whole memo with a ``PAYEE``
    token. Nothing here writes off-machine.

Column mapping is heuristic (header synonyms) with an explicit ``column_map``
escape hatch — the ``--map`` flag on ``redactor ingest`` — for headers no
heuristic could guess.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import unescape as _xml_unescape

from redactor.fixtures import Statement, Transaction


class LoadError(Exception):
    """A statement failed to parse. Carries location, never a raw value in its
    default rendering.

    The offending cell (``raw``) is stored so a human on the local machine can
    opt into seeing it via :meth:`render` with ``show_raw=True`` (the CLI's
    ``--show-raw``), but it is deliberately kept out of ``str(self)`` and the
    default :meth:`render`, so a stray log line or a copy-pasted terminal buffer
    cannot leak it.
    """

    def __init__(
        self,
        message: str,
        *,
        path: Path | str | None = None,
        row: int | None = None,
        field: str | None = None,
        raw: str | None = None,
    ) -> None:
        self.message = message
        self.path = Path(path) if path is not None else None
        self.row = row
        self.field = field
        self.raw = raw
        super().__init__(self.render(show_raw=False))

    def render(self, *, show_raw: bool = False) -> str:
        """Render the error. Value-free unless ``show_raw`` is explicitly set."""
        parts: list[str] = []
        if self.path is not None:
            parts.append(self.path.name)
        if self.row is not None:
            parts.append(f"row {self.row}")
        if self.field is not None:
            parts.append(f"field {self.field!r}")
        location = ": ".join([" ".join(parts), self.message]) if parts else self.message
        if show_raw and self.raw is not None:
            location += f" (value: {self.raw!r})"
        return location

    def __str__(self) -> str:  # value-free by construction
        return self.render(show_raw=False)


# -- CSV column heuristics ----------------------------------------------------

# Canonical field -> the header labels (normalized: lowercased, whitespace and
# punctuation collapsed) that a bank might use for it. Order within a set does
# not matter; the first matching header in the file wins.
_HEADER_SYNONYMS: dict[str, set[str]] = {
    "date": {
        "date", "posted date", "posting date", "post date", "date posted",
        "transaction date", "trans date", "effective date", "value date",
    },
    "description": {
        "description", "desc", "memo", "payee", "name", "details", "detail",
        "narrative", "merchant", "transaction", "reference", "particulars",
    },
    "amount": {
        "amount", "amt", "transaction amount", "value", "money", "total",
    },
    "debit": {
        "debit", "debit amount", "withdrawal", "withdrawals", "money out",
        "paid out", "outflow", "charge",
    },
    "credit": {
        "credit", "credit amount", "deposit", "deposits", "money in",
        "paid in", "inflow",
    },
    "category": {"category", "cat", "type", "transaction type"},
}


def _normalize_header(header: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse whitespace."""
    cleaned = re.sub(r"[^a-z0-9]+", " ", header.strip().lower())
    return " ".join(cleaned.split())


def _resolve_columns(
    headers: list[str], column_map: dict[str, str] | None
) -> dict[str, str]:
    """Map canonical field -> actual header. Explicit ``column_map`` wins.

    Raises :class:`LoadError` (value-free — header labels only) when a mapped
    header is absent or the required fields cannot be found.
    """
    present = {h for h in headers if h is not None}
    resolved: dict[str, str] = {}

    if column_map:
        for field, header in column_map.items():
            if header not in present:
                raise LoadError(
                    f"--map names column {header!r} for field {field!r}, "
                    f"but the file has no such column",
                    field=field,
                )
            resolved[field] = header

    normalized = {h: _normalize_header(h) for h in present}
    for field, synonyms in _HEADER_SYNONYMS.items():
        if field in resolved:
            continue
        for header in headers:
            if header in present and normalized[header] in synonyms:
                resolved[field] = header
                break

    if "date" not in resolved:
        raise LoadError(
            "could not identify a date column; pass --map date=<header>",
            field="date",
        )
    if "description" not in resolved:
        raise LoadError(
            "could not identify a description column; pass --map description=<header>",
            field="description",
        )
    if "amount" not in resolved and not ("debit" in resolved or "credit" in resolved):
        raise LoadError(
            "could not identify an amount column (or debit/credit pair); "
            "pass --map amount=<header>",
            field="amount",
        )
    return resolved


# -- value normalization ------------------------------------------------------

_DATE_FORMATS = (
    "%Y-%m-%d",   # ISO
    "%Y/%m/%d",
    "%m/%d/%Y",   # US slash (story: MM/DD/YYYY)
    "%m/%d/%y",
    "%m-%d-%Y",
    "%d-%b-%Y",   # 01-Apr-2026
    "%b %d, %Y",  # Apr 1, 2026
)


def _parse_date(value: str, *, path: Path, row: int, field: str) -> str:
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise LoadError(
        "unrecognized date format; expected ISO (YYYY-MM-DD) or MM/DD/YYYY",
        path=path, row=row, field=field, raw=value,
    )


_AMOUNT_STRIP = re.compile(r"[,\s $£€]")


def _parse_amount(value: str, *, path: Path, row: int, field: str) -> float:
    text = value.strip()
    negative = False
    # Accounting-style negatives: (1,234.56)
    if text.startswith("(") and text.endswith(")"):
        negative = True
        text = text[1:-1]
    # Trailing sign / CR-DR markers some banks append.
    upper = text.upper()
    if upper.endswith("CR"):
        text = text[:-2].strip()
    elif upper.endswith("DR"):
        negative = True
        text = text[:-2].strip()
    if text.endswith("-"):
        negative = True
        text = text[:-1].strip()
    text = _AMOUNT_STRIP.sub("", text)
    try:
        amount = float(text)
    except ValueError:
        raise LoadError(
            "amount is not a number", path=path, row=row, field=field, raw=value
        ) from None
    return -abs(amount) if negative else amount


def _amount_from_row(
    row: dict[str, str],
    columns: dict[str, str],
    *,
    path: Path,
    rownum: int,
) -> float:
    if "amount" in columns:
        return _parse_amount(
            row.get(columns["amount"], ""), path=path, row=rownum, field="amount"
        )
    # Debit/credit split: debit is money out (negative), credit money in.
    debit_raw = row.get(columns.get("debit", ""), "") or ""
    credit_raw = row.get(columns.get("credit", ""), "") or ""
    if debit_raw.strip():
        return -abs(_parse_amount(debit_raw, path=path, row=rownum, field="debit"))
    if credit_raw.strip():
        return abs(_parse_amount(credit_raw, path=path, row=rownum, field="credit"))
    raise LoadError(
        "row has neither a debit nor a credit value",
        path=path, row=rownum, field="debit/credit",
    )


# -- CSV ----------------------------------------------------------------------


def _decode(data: bytes) -> str:
    """Decode statement bytes, stripping a UTF-8 BOM and tolerating cp1252.

    ``utf-8-sig`` drops a leading BOM when present and is a strict UTF-8 decode
    otherwise; banks that ship Windows-1252 fall back to that (a superset of
    Latin-1 that never raises).
    """
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _parse_csv_text(
    text: str, *, path: Path, column_map: dict[str, str] | None
) -> list[Transaction]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise LoadError("file is empty; no header row", path=path)
    columns = _resolve_columns(list(reader.fieldnames), column_map)

    txns: list[Transaction] = []
    # Row 1 is the header; data rows start at line 2.
    for offset, raw_row in enumerate(reader, start=2):
        # Skip fully-blank rows some exports pad with.
        if not any((v or "").strip() for v in raw_row.values()):
            continue
        date = _parse_date(
            raw_row.get(columns["date"], ""), path=path, row=offset, field="date"
        )
        amount = _amount_from_row(raw_row, columns, path=path, rownum=offset)
        description = (raw_row.get(columns["description"], "") or "").strip()
        category = ""
        if "category" in columns:
            category = (raw_row.get(columns["category"], "") or "").strip()
        txns.append(
            Transaction(date=date, amount=amount, description=description, category=category)
        )
    return txns


# -- OFX (1.x SGML and 2.x XML) ----------------------------------------------

_OFX_TAG = re.compile(r"^<(/?)([A-Za-z0-9.]+)>(.*)$")


def _parse_ofx_text(text: str, *, path: Path) -> tuple[str, str, list[Transaction]]:
    """Parse OFX 1.x (SGML, unclosed value tags) and 2.x (XML) uniformly.

    Both dialects reduce to the same token stream once a newline is inserted
    before every ``<``: leaf values then sit alone on their line with no
    trailing close tag, and aggregate open/close tags stand by themselves. XML
    entities (``&amp;`` &c.) are unescaped; the 1.x header block and any
    ``<?xml?>``/``<?OFX?>`` processing instructions are ignored.
    """
    normalized = text.replace("<", "\n<")
    institution = ""
    account_id = ""
    txns: list[Transaction] = []
    cur: dict[str, str] | None = None
    txn_index = 0

    for seg in normalized.splitlines():
        seg = seg.strip()
        if not seg.startswith("<"):
            continue
        m = _OFX_TAG.match(seg)
        if not m:
            continue  # processing instructions (<?xml?>), comments, etc.
        closing, tag, value = m.group(1), m.group(2).upper(), _xml_unescape(m.group(3).strip())

        if tag == "STMTTRN":
            if closing:
                if cur is not None:
                    txn_index += 1
                    txns.append(_ofx_txn(cur, path=path, index=txn_index))
                cur = None
            else:
                cur = {}
            continue
        if closing:
            continue
        if cur is not None:
            cur[tag] = value
        elif tag == "ORG" and not institution:
            institution = value
        elif tag == "ACCTID" and not account_id:
            account_id = value
    return institution, account_id, txns


def _ofx_iso(ofx_date: str, *, path: Path, index: int) -> str:
    d = ofx_date.strip()[:8]
    if len(d) != 8 or not d.isdigit():
        raise LoadError(
            "OFX <DTPOSTED> is not a YYYYMMDD date",
            path=path, row=index, field="DTPOSTED", raw=ofx_date,
        )
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


def _ofx_txn(cur: dict[str, str], *, path: Path, index: int) -> Transaction:
    amount_raw = cur.get("TRNAMT", "")
    try:
        amount = float(amount_raw)
    except ValueError:
        raise LoadError(
            "OFX <TRNAMT> is not a number",
            path=path, row=index, field="TRNAMT", raw=amount_raw,
        ) from None
    return Transaction(
        date=_ofx_iso(cur.get("DTPOSTED", ""), path=path, index=index),
        amount=amount,
        description=cur.get("NAME", "").strip(),
        ttype=cur.get("TRNTYPE", ""),
        fitid=cur.get("FITID", ""),
    )


# -- format detection + public entry point -----------------------------------


def _is_ofx(text: str, suffix: str) -> bool:
    if suffix in (".ofx", ".qfx"):
        return True
    head = text.lstrip()[:512].upper()
    return head.startswith("OFXHEADER") or "<OFX>" in head or head.startswith("<?OFX")


def load_statement_file(
    path: Path | str,
    *,
    column_map: dict[str, str] | None = None,
    account_id: str | None = None,
    institution: str | None = None,
) -> Statement:
    """Load one statement file into a :class:`~redactor.fixtures.Statement`.

    Args:
        path: the CSV or OFX file to load.
        column_map: explicit ``{canonical_field: header}`` overrides for CSV
            column resolution (the ``--map`` escape hatch). Ignored for OFX.
        account_id / institution: metadata overrides. OFX carries both in-file;
            CSV does not, so these default to the filename stem for the account
            and to the account id for the institution when not supplied.

    Raises:
        LoadError: the file could not be parsed. Its default rendering is
            value-free; pass ``show_raw=True`` to :meth:`LoadError.render` on the
            local machine to see the offending cell.
    """
    p = Path(path)
    try:
        data = p.read_bytes()
    except OSError as exc:
        raise LoadError(f"cannot read file: {exc.strerror}", path=p) from exc
    text = _decode(data)
    suffix = p.suffix.lower()

    if _is_ofx(text, suffix):
        file_inst, file_acct, txns = _parse_ofx_text(text, path=p)
        fmt = "ofx"
        resolved_acct = account_id or file_acct or p.stem
        resolved_inst = institution or file_inst or resolved_acct
    else:
        txns = _parse_csv_text(text, path=p, column_map=column_map)
        fmt = "csv"
        resolved_acct = account_id or p.stem
        resolved_inst = institution or resolved_acct

    # Best-effort month from the first transaction (YYYY-MM); metadata only.
    month = txns[0].date[:7] if txns else ""
    return Statement(
        account_type="",
        format=fmt,
        month=month,
        institution=resolved_inst,
        account_id=resolved_acct,
        source=p,
        transactions=txns,
    )
