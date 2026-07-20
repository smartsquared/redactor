#!/usr/bin/env python3
"""Deterministic generator for the synthetic bookkeeping fixtures (story S0.2).

Run from anywhere:

    python tests/fixtures/bookkeeping/generate_fixtures.py

It (re)writes the CSV + OFX statement files and ``manifest.json`` in this
directory. The generator is the *source of truth* for the payee-variant
ground truth: each canonical payee has several deliberately-different memo
strings ("WHOLEFDS #1029 SEA" vs "WF MKT 445") so downstream entity-resolution
work (S1.3) and the leak-check harness (S2.4) have something real to chew on.

**Everything here is provably fake** — see ``docs/alias-contract.md`` and the
fake-ness lint (``redactor/fakeness.py``):

* Institutions are fictional ("Bank of Nowhere", "Placeholder National Bank").
* The routing number ``123456789`` deliberately FAILS the ABA checksum.
* Account ids are alphanumeric and match no real bank format.
* No memo, amount, or date contains a Luhn-valid card number, an ABA-valid
  routing number, or an SSN-shaped string.

There is no randomness on purpose: byte-for-byte reproducible output means the
committed fixtures and this script can never silently drift.
"""
from __future__ import annotations

import csv
import io
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# --- Payee variant table -------------------------------------------------
#
# canonical name -> list of raw memo variants seen across statements.
# Downstream S1.3 must resolve every variant of a canonical payee to a single
# PAYEE-n alias. At least 10 payees carry >1 variant (accept criterion).
PAYEE_VARIANTS: dict[str, list[str]] = {
    "Whole Foods Market": ["WHOLEFDS #1029 SEA", "WF MKT 445 SEATTLE WA", "WHOLE FOODS MKT #10029"],
    "Starbucks": ["STARBUCKS #5561", "SBUX 8842 SEATTLE WA", "STARBUCKS STORE 05561 SEA"],
    "Amazon": ["AMZN Mktp US*RT4X9", "AMAZON.COM*MK12QP", "Amazon.com AMZN.COM/BILL WA"],
    "Shell": ["SHELL OIL 5712341", "SHELL SERVICE STATION #041", "SHELL 12345678 KENT WA"],
    "Netflix": ["NETFLIX.COM", "NETFLIX 8663 LOS GATOS", "Netflix.com CA"],
    "Comcast Xfinity": ["COMCAST CABLE COMM", "XFINITY 800 COMCAST", "COMCAST*CABLE 8493"],
    "Trader Joe's": ["TRADER JOE'S #130", "TRADER JOES 130 QPS", "TJ'S #0130 SEATTLE"],
    "Target": ["TARGET T-1123", "TARGET 00011234 SEA", "TARGET.COM *ORDER"],
    "Costco": ["COSTCO WHSE #0044", "COSTCO GAS #0044 KENT", "COSTCO WHOLESALE 44"],
    "Chipotle": ["CHIPOTLE 2245", "CHIPOTLE MEXICAN GRILL 2245", "CHIPOTLE ONLINE"],
    "Uber": ["UBER *TRIP HELP.UBER", "UBER EATS 8005", "UBER*EATS SAN FRANCISCO"],
    "Seattle City Light": ["SEATTLE CITY LIGHT", "SCL UTILITY BILL", "CITY LIGHT ONLINE PMT"],
    # Single-variant fixed-string payees (income / transfers).
    "Fictional Corp Payroll": ["FICTIONAL CORP PAYROLL PPD"],
    "Testburg Property Mgmt": ["TESTBURG PROPERTY MGMT RENT"],
    "Card Payment": ["AUTOPAY PAYMENT - THANK YOU"],
}


def variant(payee: str, month_index: int) -> str:
    """Pick a memo variant for a payee, rotating by month so a canonical payee
    shows up under a *different* raw string each month."""
    variants = PAYEE_VARIANTS[payee]
    return variants[month_index % len(variants)]


MONTHS = ["2026-04", "2026-05", "2026-06"]


def month_bounds(month: str) -> tuple[str, str]:
    year, mm = month.split("-")
    last = {"04": "30", "05": "31", "06": "30"}[mm]
    return f"{year}{mm}01", f"{year}{mm}{last}"


def day(month: str, d: int) -> str:
    return month.replace("-", "") + f"{d:02d}"


# --- Account definitions -------------------------------------------------

CHECKING = {
    "institution": "Bank of Nowhere",
    "org": "Bank of Nowhere",
    "fid": "9999",
    "bankid": "123456789",  # provably fake: FAILS the ABA checksum
    "acctid": "NOWHERE-CHK-000199",
    "accttype": "CHECKING",
    "display_mask": "••••0199",
}

CREDIT = {
    "institution": "Placeholder National Bank",
    "org": "Placeholder National Bank",
    "fid": "8888",
    "acctid": "PLACEHOLDER-CC-000042",
    "accttype": "CREDITLINE",
    "display_mask": "••••0042",
}


def checking_txns(month: str, mi: int) -> list[dict]:
    """(day, ttype, amount, canonical_payee) for the checking account."""
    def v(p: str) -> str:
        return variant(p, mi)
    rows = [
        (1, "DEBIT", "-1850.00", "Testburg Property Mgmt", v("Testburg Property Mgmt")),
        (3, "CREDIT", "3120.44", "Fictional Corp Payroll", v("Fictional Corp Payroll")),
        (5, "DEBIT", "-52.18", "Whole Foods Market", v("Whole Foods Market")),
        (9, "DEBIT", "-41.77", "Trader Joe's", v("Trader Joe's")),
        (12, "DEBIT", "-88.03", "Costco", v("Costco")),
        (14, "DEBIT", "-61.40", "Shell", v("Shell")),
        (17, "CREDIT", "3120.44", "Fictional Corp Payroll", v("Fictional Corp Payroll")),
        (18, "DEBIT", "-134.92", "Seattle City Light", v("Seattle City Light")),
        (21, "DEBIT", "-96.15", "Comcast Xfinity", v("Comcast Xfinity")),
        (25, "DEBIT", "-450.00", "Card Payment", v("Card Payment")),
        (28, "DEBIT", "-33.61", "Whole Foods Market",
         PAYEE_VARIANTS["Whole Foods Market"][(mi + 1) % 3]),
    ]
    return [
        {"day": day(month, d), "ttype": t, "amount": a, "canonical": c, "memo": m}
        for (d, t, a, c, m) in rows
    ]


def credit_txns(month: str, mi: int) -> list[dict]:
    """(day, ttype, amount, canonical_payee) for the credit-card account."""
    def v(p: str) -> str:
        return variant(p, mi)
    rows = [
        (2, "DEBIT", "-5.75", "Starbucks", v("Starbucks")),
        (4, "DEBIT", "-73.21", "Amazon", v("Amazon")),
        (6, "DEBIT", "-15.99", "Netflix", v("Netflix")),
        (8, "DEBIT", "-42.10", "Target", v("Target")),
        (10, "DEBIT", "-12.84", "Chipotle", v("Chipotle")),
        (13, "DEBIT", "-27.50", "Uber", v("Uber")),
        (16, "DEBIT", "-58.30", "Costco", PAYEE_VARIANTS["Costco"][1]),  # COSTCO GAS
        (19, "DEBIT", "-6.25", "Starbucks", PAYEE_VARIANTS["Starbucks"][(mi + 2) % 3]),
        (22, "DEBIT", "-51.09", "Shell", v("Shell")),
        (26, "CREDIT", "450.00", "Card Payment", v("Card Payment")),
    ]
    return [
        {"day": day(month, d), "ttype": t, "amount": a, "canonical": c, "memo": m}
        for (d, t, a, c, m) in rows
    ]


# --- Renderers -----------------------------------------------------------

def render_checking_csv(month: str, txns: list[dict]) -> str:
    """Bank-of-Nowhere export style: Date,Description,Amount,Balance."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Date", "Description", "Amount", "Balance"])
    balance = 500.00
    for t in txns:
        balance += float(t["amount"])
        d = t["day"]
        iso = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
        w.writerow([iso, t["memo"], t["amount"], f"{balance:.2f}"])
    return buf.getvalue()


def render_credit_csv(month: str, txns: list[dict]) -> str:
    """Placeholder-National export style: Date,Description,Category,Amount."""
    cat = {
        "Starbucks": "Dining", "Amazon": "Shopping", "Netflix": "Entertainment",
        "Target": "Shopping", "Chipotle": "Dining", "Uber": "Travel",
        "Costco": "Groceries", "Shell": "Gas", "Card Payment": "Payment",
    }
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["Date", "Description", "Category", "Amount"])
    for t in txns:
        d = t["day"]
        iso = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
        w.writerow([iso, t["memo"], cat.get(t["canonical"], "Other"), t["amount"]])
    return buf.getvalue()


def render_ofx(acct: dict, month: str, txns: list[dict], kind: str) -> str:
    """OFX 1.0.2 SGML — the format most consumer banks actually export.

    Value tags are intentionally left unclosed (valid for OFX 1.x SGML); the
    loader in ``redactor/fixtures.py`` parses this tolerantly.
    """
    dtstart, dtend = month_bounds(month)
    if kind == "checking":
        msgset, trnrs, stmtrs = "BANKMSGSRSV1", "STMTTRNRS", "STMTRS"
        acctfrom = (
            "<BANKACCTFROM>\n"
            f"<BANKID>{acct['bankid']}\n"
            f"<ACCTID>{acct['acctid']}\n"
            f"<ACCTTYPE>{acct['accttype']}\n"
            "</BANKACCTFROM>"
        )
    else:
        msgset, trnrs, stmtrs = "CREDITCARDMSGSRSV1", "CCSTMTTRNRS", "CCSTMTRS"
        acctfrom = (
            "<CCACCTFROM>\n"
            f"<ACCTID>{acct['acctid']}\n"
            "</CCACCTFROM>"
        )

    lines = [
        "OFXHEADER:100",
        "DATA:OFXSGML",
        "VERSION:102",
        "SECURITY:NONE",
        "ENCODING:USASCII",
        "CHARSET:1252",
        "COMPRESSION:NONE",
        "OLDFILEUID:NONE",
        "NEWFILEUID:NONE",
        "",
        "<OFX>",
        "<SIGNONMSGSRSV1>",
        "<SONRS>",
        "<STATUS>",
        "<CODE>0",
        "<SEVERITY>INFO",
        "</STATUS>",
        f"<DTSERVER>{dtend}",
        "<LANGUAGE>ENG",
        "<FI>",
        f"<ORG>{acct['org']}",
        f"<FID>{acct['fid']}",
        "</FI>",
        "</SONRS>",
        "</SIGNONMSGSRSV1>",
        f"<{msgset}>",
        f"<{trnrs}>",
        "<TRNUID>1001",
        "<STATUS>",
        "<CODE>0",
        "<SEVERITY>INFO",
        "</STATUS>",
        f"<{stmtrs}>",
        "<CURDEF>USD",
        acctfrom,
        "<BANKTRANLIST>",
        f"<DTSTART>{dtstart}",
        f"<DTEND>{dtend}",
    ]
    balance = 0.0
    for i, t in enumerate(txns):
        balance += float(t["amount"])
        fitid = f"{'CHK' if kind == 'checking' else 'CC'}{t['day']}{i:03d}"
        lines += [
            "<STMTTRN>",
            f"<TRNTYPE>{t['ttype']}",
            f"<DTPOSTED>{t['day']}",
            f"<TRNAMT>{t['amount']}",
            f"<FITID>{fitid}",
            f"<NAME>{t['memo']}",
            "</STMTTRN>",
        ]
    lines += [
        "</BANKTRANLIST>",
        "<LEDGERBAL>",
        f"<BALAMT>{balance:.2f}",
        f"<DTASOF>{dtend}",
        "</LEDGERBAL>",
        f"</{stmtrs}>",
        f"</{trnrs}>",
        f"</{msgset}>",
        "</OFX>",
        "",
    ]
    return "\n".join(lines)


def write(path: str, content: str) -> None:
    with open(os.path.join(HERE, path), "w", encoding="utf-8") as fh:
        fh.write(content)
    print(f"wrote {path}")


def main() -> None:
    manifest_payees: dict[str, list[str]] = {k: sorted(set(v)) for k, v in PAYEE_VARIANTS.items()}
    files: list[str] = []

    for mi, month in enumerate(MONTHS):
        c_txns = checking_txns(month, mi)
        cc_txns = credit_txns(month, mi)

        for name, content in [
            (f"checking_{month}.csv", render_checking_csv(month, c_txns)),
            (f"checking_{month}.ofx", render_ofx(CHECKING, month, c_txns, "checking")),
            (f"credit_{month}.csv", render_credit_csv(month, cc_txns)),
            (f"credit_{month}.ofx", render_ofx(CREDIT, month, cc_txns, "credit")),
        ]:
            write(name, content)
            files.append(name)

    manifest = {
        "note": "Synthetic bookkeeping fixtures. Provably fake. See generate_fixtures.py.",
        "months": MONTHS,
        "accounts": {
            "checking": {
                "institution": CHECKING["institution"],
                "account_id": CHECKING["acctid"],
                "routing_number": CHECKING["bankid"],
                "routing_note": "fails ABA checksum on purpose",
                "display_mask": CHECKING["display_mask"],
            },
            "credit": {
                "institution": CREDIT["institution"],
                "account_id": CREDIT["acctid"],
                "display_mask": CREDIT["display_mask"],
            },
        },
        "multi_variant_payee_count": sum(1 for v in PAYEE_VARIANTS.values() if len(v) > 1),
        "payees": manifest_payees,
        "files": sorted(files),
    }
    write("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
