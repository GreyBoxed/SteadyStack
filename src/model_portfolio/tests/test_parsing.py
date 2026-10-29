"""Model portfolio validation.

Every case here is a file somebody will really upload. The ones worth reading
twice: three equal thirds must be accepted, and a file with several problems
must report all of them -- an operator fixing an upload should not have to
discover the mistakes one round trip at a time.
"""

from decimal import Decimal

import pytest

from model_portfolio.parsing import WEIGHT_TOLERANCE, parse, resolve_instruments
from portfolio.instruments import refresh


def master(symbol, token):
    return {
        "tradingsymbol": symbol,
        "instrument_token": token,
        "name": symbol,
        "exchange": "NSE",
        "instrument_type": "EQ",
        "segment": "NSE",
        "tick_size": 0.05,
        "lot_size": 1,
    }


@pytest.fixture
def universe(db):
    refresh([master("INFY", 408065), master("TCS", 2953217), master("HDFCBANK", 341249)])


CLEAN = "symbol,target_pct\nINFY,40\nTCS,35\nHDFCBANK,25\n"


# -- the happy path --------------------------------------------------------


def test_a_clean_file_parses():
    result = parse(CLEAN)
    assert result.ok is True
    assert [row.symbol for row in result.rows] == ["INFY", "TCS", "HDFCBANK"]


def test_weights_are_read_as_decimals_not_floats():
    """Money arithmetic downstream is exact; floats here would poison it."""
    result = parse("symbol,target_pct\nINFY,33.33\nTCS,33.33\nHDFCBANK,33.34\n")
    assert result.rows[0].target_pct == Decimal("33.33")


def test_three_equal_thirds_are_accepted():
    """33.33 x 3 = 99.99. Refusing this refuses the most obvious portfolio
    anybody can describe."""
    result = parse("symbol,target_pct\nINFY,33.33\nTCS,33.33\nHDFCBANK,33.33\n")
    assert result.ok is True
    assert result.total == Decimal("99.99")


def test_a_single_name_at_one_hundred_is_accepted():
    assert parse("symbol,target_pct\nINFY,100\n").ok is True


# -- shapes a spreadsheet produces ----------------------------------------


def test_a_byte_order_mark_does_not_break_the_header():
    """Excel writes one. Without handling it the first column never matches."""
    assert parse("﻿symbol,target_pct\nINFY,100\n").ok is True


def test_a_utf8_bom_in_bytes_is_handled():
    assert parse(b"\xef\xbb\xbfsymbol,target_pct\nINFY,100\n").ok is True


def test_lower_case_symbols_are_accepted():
    result = parse("symbol,target_pct\ninfy,100\n")
    assert result.rows[0].symbol == "INFY"


def test_surrounding_whitespace_is_stripped():
    result = parse("symbol,target_pct\n  INFY  ,  100  \n")
    assert result.rows[0].symbol == "INFY"
    assert result.rows[0].target_pct == Decimal("100")


def test_a_percent_sign_is_tolerated():
    assert parse("symbol,target_pct\nINFY,100%\n").rows[0].target_pct == Decimal("100")


def test_a_trailing_blank_line_is_not_an_error():
    assert parse("symbol,target_pct\nINFY,100\n\n").ok is True


def test_alternative_header_names_are_understood():
    assert parse("ticker,weight\nINFY,100\n").ok is True


def test_carriage_returns_are_handled():
    assert parse("symbol,target_pct\r\nINFY,100\r\n").ok is True


# -- refusals --------------------------------------------------------------


def test_an_empty_file_is_refused():
    assert "empty" in parse("").errors[0].lower()


def test_a_header_with_no_rows_is_refused():
    assert "no rows" in parse("symbol,target_pct\n").errors[0]


def test_an_unrecognisable_header_is_refused():
    assert "header" in parse("name,amount\nINFY,100\n").errors[0].lower()


def test_weights_that_do_not_sum_to_one_hundred_are_refused():
    result = parse("symbol,target_pct\nINFY,40\nTCS,40\n")
    assert any("sum to 80" in error for error in result.errors)


def test_weights_just_outside_the_tolerance_are_refused():
    over = Decimal("100") + WEIGHT_TOLERANCE + Decimal("0.01")
    result = parse(f"symbol,target_pct\nINFY,{over}\n")
    assert result.ok is False


def test_a_duplicate_symbol_is_refused():
    result = parse("symbol,target_pct\nINFY,50\nINFY,50\n")
    assert any("appears twice" in error for error in result.errors)


def test_a_duplicate_is_caught_across_casing():
    result = parse("symbol,target_pct\nINFY,50\ninfy,50\n")
    assert any("appears twice" in error for error in result.errors)


def test_a_zero_weight_is_refused():
    """Zero is not "hold none of it" -- it is a name that should not be here."""
    result = parse("symbol,target_pct\nINFY,100\nTCS,0\n")
    assert any("above zero" in error for error in result.errors)


def test_a_negative_weight_is_refused():
    result = parse("symbol,target_pct\nINFY,110\nTCS,-10\n")
    assert any("above zero" in error for error in result.errors)


def test_a_weight_above_one_hundred_is_refused():
    result = parse("symbol,target_pct\nINFY,150\n")
    assert any("above 100" in error for error in result.errors)


def test_a_non_numeric_weight_is_refused():
    result = parse("symbol,target_pct\nINFY,a lot\n")
    assert any("not a number" in error for error in result.errors)


def test_a_missing_weight_is_refused():
    result = parse("symbol,target_pct\nINFY,\n")
    assert any("no weight" in error for error in result.errors)


def test_a_missing_symbol_is_refused():
    result = parse("symbol,target_pct\n,100\n")
    assert any("no symbol" in error for error in result.errors)


def test_every_problem_is_reported_not_just_the_first():
    """One round trip per mistake is how an upload screen becomes hated."""
    result = parse("symbol,target_pct\nINFY,0\nTCS,nonsense\nTCS,50\n")
    assert len(result.errors) >= 3


def test_the_line_number_is_the_one_in_the_file():
    """Line 1 is the header, so the first data row is line 2."""
    result = parse("symbol,target_pct\nINFY,0\n")
    assert "Line 2" in result.errors[0]


# -- content hashing -------------------------------------------------------


def test_the_same_portfolio_in_a_different_order_hashes_the_same():
    """It is the same instruction; re-issuing it re-ordered changes nothing."""
    first = parse("symbol,target_pct\nINFY,40\nTCS,60\n")
    second = parse("symbol,target_pct\nTCS,60\nINFY,40\n")
    assert first.content_hash == second.content_hash


def test_a_changed_weight_changes_the_hash():
    first = parse("symbol,target_pct\nINFY,40\nTCS,60\n")
    second = parse("symbol,target_pct\nINFY,41\nTCS,59\n")
    assert first.content_hash != second.content_hash


def test_casing_and_line_endings_do_not_change_the_hash():
    first = parse("symbol,target_pct\nINFY,40\nTCS,60\n")
    second = parse("symbol,target_pct\r\ninfy,40\r\ntcs,60\r\n")
    assert first.content_hash == second.content_hash


# -- against the instrument master ----------------------------------------


@pytest.mark.django_db
def test_known_symbols_resolve(universe):
    result = parse(CLEAN)
    resolve_instruments(result)
    assert result.ok is True


@pytest.mark.django_db
def test_an_unknown_symbol_is_refused(universe):
    result = parse("symbol,target_pct\nINFY,50\nNOSUCHCO,50\n")
    resolve_instruments(result)
    assert any("not in the instrument master" in error for error in result.errors)


@pytest.mark.django_db
def test_a_delisted_symbol_is_refused_with_its_own_message(universe):
    """A typo and a delisting need different fixes, so they read differently."""
    from portfolio.models import Instrument

    Instrument.objects.filter(pk="TCS").update(active=False)

    result = parse(CLEAN)
    resolve_instruments(result)

    assert any("no longer trading" in error for error in result.errors)
