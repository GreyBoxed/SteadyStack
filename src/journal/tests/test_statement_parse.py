from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from journal.statement.parse import StatementError, parse

FIXTURES = Path(__file__).parent / "fixtures"


def test_a_statement_parses_to_rows_and_balances():
    statement = parse((FIXTURES / "statement_aug.csv").read_bytes())
    assert statement.opening == Decimal("-150.14")
    assert statement.closing == Decimal("11.568916")
    assert len(statement.rows) == 4
    first = statement.rows[0]
    assert first.posting_date == date(2026, 8, 5)
    assert first.debit == Decimal("1.57")
    assert first.voucher_type == "Journal Entry"


def test_the_same_row_has_the_same_fingerprint_in_two_files():
    aug = parse((FIXTURES / "statement_aug.csv").read_bytes())
    sep = parse((FIXTURES / "statement_sep.csv").read_bytes())
    assert aug.rows[-1].fingerprint == sep.rows[0].fingerprint


def test_identical_rows_on_one_day_get_different_fingerprints():
    raw = (
        "particulars,posting_date,cost_center,voucher_type,"
        "debit,credit,net_balance\n"
        "Opening Balance,,,,,,0\n"
        "DP charges,2026-09-01,NSE-EQ - Z,Journal Entry,"
        "15.34,0,-15.34\n"
        "DP charges,2026-09-01,NSE-EQ - Z,Journal Entry,"
        "15.34,0,-30.68\n"
        "Closing Balance,,,,,,-30.68\n"
    )
    rows = parse(raw).rows
    assert rows[0].fingerprint != rows[1].fingerprint


def test_a_running_balance_that_does_not_add_up_is_refused():
    raw = (
        "particulars,posting_date,cost_center,voucher_type,"
        "debit,credit,net_balance\n"
        "Opening Balance,,,,,,0\n"
        "AMC,2026-09-01,NSE-EQ - Z,Journal Entry,10,0,-11\n"
        "Closing Balance,,,,,,-11\n"
    )
    with pytest.raises(StatementError, match="line 1"):
        parse(raw)


def test_a_file_with_the_wrong_columns_is_refused():
    with pytest.raises(StatementError, match="columns"):
        parse("date,amount\n2026-09-01,10\n")


def test_a_file_without_its_balance_rows_is_refused():
    with pytest.raises(StatementError, match="Opening Balance"):
        parse("particulars,posting_date,cost_center,voucher_type,debit,credit,net_balance\n")


def test_a_padded_header_parses_correctly():
    raw = (
        "particulars ,posting_date,cost_center,voucher_type,"
        "debit,credit,net_balance\n"
        "Opening Balance,,,,,,0\n"
        "AMC,2026-09-01,NSE-EQ - Z,Journal Entry,10,0,-10\n"
        "Closing Balance,,,,,,-10\n"
    )
    statement = parse(raw)
    assert statement.opening == Decimal("0")
    assert statement.closing == Decimal("-10")
    assert len(statement.rows) == 1


def test_multiple_opening_balance_rows_are_refused():
    raw = (
        "particulars,posting_date,cost_center,voucher_type,"
        "debit,credit,net_balance\n"
        "Opening Balance,,,,,,0\n"
        "Opening Balance,,,,,,0\n"
        "Closing Balance,,,,,,-10\n"
    )
    with pytest.raises(StatementError, match="more than one Opening Balance"):
        parse(raw)


def test_multiple_closing_balance_rows_are_refused():
    raw = (
        "particulars,posting_date,cost_center,voucher_type,"
        "debit,credit,net_balance\n"
        "Opening Balance,,,,,,0\n"
        "Closing Balance,,,,,,-10\n"
        "Closing Balance,,,,,,-10\n"
    )
    with pytest.raises(StatementError, match="more than one Closing Balance"):
        parse(raw)
