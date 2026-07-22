"""Tolerant statement loading for real bank exports (story S0.3).

Real exports are messier than the bundled fixtures: header names vary, files
carry a UTF-8 BOM, dates come as ``MM/DD/YYYY``, extra columns ride along, and
OFX ships in both the 1.x SGML dialect and the 2.x XML dialect. These tests pin
the hardened loader's contract:

  * fixture files still load unchanged;
  * deliberately-mangled variants ingest anyway (or fail with a value-free,
    actionable error — never echoing the offending cell into copy-pasteable
    terminal output).

Nothing here touches real data — every crafted input is provably fake.
"""
from __future__ import annotations

import pytest

from redactor.fixtures import FIXTURES_DIR
from redactor.loaders import LoadError, load_statement_file

CHECKING_CSV = FIXTURES_DIR / "checking_2026-04.csv"
CREDIT_CSV = FIXTURES_DIR / "credit_2026-04.csv"
CREDIT_OFX = FIXTURES_DIR / "credit_2026-04.ofx"
CHECKING_OFX = FIXTURES_DIR / "checking_2026-04.ofx"


def test_loads_fixture_checking_csv():
    stmt = load_statement_file(CHECKING_CSV)
    assert stmt.format == "csv"
    assert len(stmt.transactions) == 11
    first = stmt.transactions[0]
    assert first.date == "2026-04-01"          # already ISO
    assert first.amount == -1850.00
    assert first.description == "TESTBURG PROPERTY MGMT RENT"


def test_loads_fixture_credit_csv_with_reordered_columns():
    # credit CSV is Date,Description,Category,Amount — Amount is NOT the last
    # column and Category rides in the middle. Heuristic column mapping must
    # still find the right fields.
    stmt = load_statement_file(CREDIT_CSV)
    assert len(stmt.transactions) == 10
    assert stmt.transactions[0].amount == -5.75
    assert stmt.transactions[0].category == "Dining"


def test_bom_prefixed_csv_loads(tmp_path):
    raw = CHECKING_CSV.read_bytes()
    mangled = tmp_path / "bom.csv"
    mangled.write_bytes(b"\xef\xbb\xbf" + raw)  # UTF-8 BOM
    stmt = load_statement_file(mangled)
    assert len(stmt.transactions) == 11
    assert stmt.transactions[0].description == "TESTBURG PROPERTY MGMT RENT"


def test_extra_columns_are_ignored(tmp_path):
    mangled = tmp_path / "extra.csv"
    mangled.write_text(
        "Date,Description,Amount,Balance,RunningTotal,Notes\n"
        "2026-04-01,TESTBURG PROPERTY MGMT RENT,-1850.00,-1350.00,x,hello\n",
        encoding="utf-8",
    )
    stmt = load_statement_file(mangled)
    assert len(stmt.transactions) == 1
    assert stmt.transactions[0].amount == -1850.00


def test_us_slash_dates_are_normalized_to_iso(tmp_path):
    mangled = tmp_path / "usdate.csv"
    mangled.write_text(
        "Date,Description,Amount\n"
        "04/01/2026,TESTBURG PROPERTY MGMT RENT,-1850.00\n"
        "12/31/2026,COMCAST CABLE COMM,-96.15\n",
        encoding="utf-8",
    )
    stmt = load_statement_file(mangled)
    assert [t.date for t in stmt.transactions] == ["2026-04-01", "2026-12-31"]


def test_header_synonyms_are_recognized(tmp_path):
    mangled = tmp_path / "synonyms.csv"
    mangled.write_text(
        "Posted Date,Memo,Amt\n"
        "2026-04-01,TESTBURG PROPERTY MGMT RENT,-1850.00\n",
        encoding="utf-8",
    )
    stmt = load_statement_file(mangled)
    assert stmt.transactions[0].description == "TESTBURG PROPERTY MGMT RENT"
    assert stmt.transactions[0].amount == -1850.00


def test_debit_credit_columns_combine_into_signed_amount(tmp_path):
    mangled = tmp_path / "debcred.csv"
    mangled.write_text(
        "Date,Description,Debit,Credit\n"
        "2026-04-01,TESTBURG PROPERTY MGMT RENT,1850.00,\n"
        "2026-04-03,FICTIONAL CORP PAYROLL PPD,,3120.44\n",
        encoding="utf-8",
    )
    stmt = load_statement_file(mangled)
    assert stmt.transactions[0].amount == -1850.00   # debit -> money out
    assert stmt.transactions[1].amount == 3120.44    # credit -> money in


def test_currency_symbols_and_parens_negatives(tmp_path):
    mangled = tmp_path / "money.csv"
    mangled.write_text(
        "Date,Description,Amount\n"
        "2026-04-01,TESTBURG PROPERTY MGMT RENT,\"($1,850.00)\"\n"
        "2026-04-03,FICTIONAL CORP PAYROLL PPD,\"$3,120.44\"\n",
        encoding="utf-8",
    )
    stmt = load_statement_file(mangled)
    assert stmt.transactions[0].amount == -1850.00
    assert stmt.transactions[1].amount == 3120.44


def test_explicit_map_overrides_heuristics(tmp_path):
    # Headers a heuristic could never guess -> the --map escape hatch names them.
    mangled = tmp_path / "opaque.csv"
    mangled.write_text(
        "col_a,col_b,col_c\n"
        "2026-04-01,TESTBURG PROPERTY MGMT RENT,-1850.00\n",
        encoding="utf-8",
    )
    stmt = load_statement_file(
        mangled,
        column_map={"date": "col_a", "description": "col_b", "amount": "col_c"},
    )
    assert stmt.transactions[0].date == "2026-04-01"
    assert stmt.transactions[0].amount == -1850.00


def test_unmappable_headers_fail_value_free(tmp_path):
    mangled = tmp_path / "opaque.csv"
    mangled.write_text(
        "col_a,col_b,col_c\n"
        "2026-04-01,SEKRET-MERCHANT-XYZ,-1850.00\n",
        encoding="utf-8",
    )
    with pytest.raises(LoadError) as exc:
        load_statement_file(mangled)
    # The error names a missing column and the --map remedy, not any cell value.
    rendered = exc.value.render()
    assert "SEKRET-MERCHANT-XYZ" not in rendered
    assert "--map" in rendered
    assert any(f in rendered.lower() for f in ("date", "amount", "description"))


def test_loads_fixture_ofx_1x():
    stmt = load_statement_file(CREDIT_OFX)
    assert stmt.institution == "Placeholder National Bank"
    assert len(stmt.transactions) == 10
    assert stmt.transactions[0].description == "STARBUCKS #5561"
    assert stmt.transactions[0].amount == -5.75


def test_ofx_account_id_read_from_file():
    stmt = load_statement_file(CREDIT_OFX)
    assert stmt.account_id == "PLACEHOLDER-CC-000042"


# A provably-fake OFX 2.x (XML) document: closing tags, an XML declaration, and
# an escaped ampersand in a memo. Same shape as the 1.x fixtures.
OFX_2X = """<?xml version="1.0" encoding="UTF-8"?>
<?OFX OFXHEADER="200" VERSION="211" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>
<OFX>
  <SIGNONMSGSRSV1><SONRS>
    <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
    <DTSERVER>20260430</DTSERVER>
    <FI><ORG>Placeholder National Bank</ORG><FID>8888</FID></FI>
  </SONRS></SIGNONMSGSRSV1>
  <CREDITCARDMSGSRSV1><CCSTMTTRNRS>
    <TRNUID>1001</TRNUID>
    <STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>
    <CCSTMTRS>
      <CURDEF>USD</CURDEF>
      <CCACCTFROM><ACCTID>PLACEHOLDER-CC-000042</ACCTID></CCACCTFROM>
      <BANKTRANLIST>
        <DTSTART>20260401</DTSTART><DTEND>20260430</DTEND>
        <STMTTRN>
          <TRNTYPE>DEBIT</TRNTYPE>
          <DTPOSTED>20260402</DTPOSTED>
          <TRNAMT>-5.75</TRNAMT>
          <FITID>CC20260402000</FITID>
          <NAME>STARBUCKS #5561</NAME>
        </STMTTRN>
        <STMTTRN>
          <TRNTYPE>DEBIT</TRNTYPE>
          <DTPOSTED>20260406</DTPOSTED>
          <TRNAMT>-15.99</TRNAMT>
          <FITID>CC20260406002</FITID>
          <NAME>NETFLIX.COM &amp; CHILL</NAME>
        </STMTTRN>
      </BANKTRANLIST>
    </CCSTMTRS>
  </CCSTMTTRNRS></CREDITCARDMSGSRSV1>
</OFX>
"""


def test_loads_ofx_2x_xml(tmp_path):
    path = tmp_path / "credit_v2.ofx"
    path.write_text(OFX_2X, encoding="utf-8")
    stmt = load_statement_file(path)
    assert stmt.account_id == "PLACEHOLDER-CC-000042"
    assert stmt.institution == "Placeholder National Bank"
    assert len(stmt.transactions) == 2
    assert stmt.transactions[0].description == "STARBUCKS #5561"
    assert stmt.transactions[0].amount == -5.75
    # XML entity in the memo is unescaped.
    assert stmt.transactions[1].description == "NETFLIX.COM & CHILL"


def test_bad_amount_cell_error_hides_value_by_default(tmp_path):
    mangled = tmp_path / "badamt.csv"
    mangled.write_text(
        "Date,Description,Amount\n"
        "2026-04-01,TESTBURG PROPERTY MGMT RENT,SEKRET-AMOUNT-9\n",
        encoding="utf-8",
    )
    with pytest.raises(LoadError) as exc:
        load_statement_file(mangled)
    err = exc.value
    # Row + field name are safe to show; the raw cell value is not.
    assert "SEKRET-AMOUNT-9" not in err.render()
    assert "SEKRET-AMOUNT-9" not in str(err)
    assert "row 2" in err.render().lower() or "row" in err.render().lower()
    assert "amount" in err.render().lower()
    # ...but the value is available behind the explicit show_raw gate.
    assert "SEKRET-AMOUNT-9" in err.render(show_raw=True)


def test_bad_date_cell_error_is_value_free(tmp_path):
    mangled = tmp_path / "baddate.csv"
    mangled.write_text(
        "Date,Description,Amount\n"
        "NOT-A-DATE-XYZ,TESTBURG PROPERTY MGMT RENT,-1850.00\n",
        encoding="utf-8",
    )
    with pytest.raises(LoadError) as exc:
        load_statement_file(mangled)
    assert "NOT-A-DATE-XYZ" not in exc.value.render()
    assert "date" in exc.value.render().lower()
    assert "NOT-A-DATE-XYZ" in exc.value.render(show_raw=True)
