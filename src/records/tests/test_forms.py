"""Forms that record money refuse a date that has not happened yet (M12)."""

import uuid
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def on_the_25th(time_machine):
    time_machine.move_to(datetime(2026, 9, 25, 23, 30, tzinfo=IST))


def _lumpsum(received_on):
    from records.forms import LumpsumForm

    return LumpsumForm(
        {
            "amount": "5000",
            "received_on": received_on.isoformat(),
            "reference": "111111111111",
            "submission": uuid.uuid4().hex,
        }
    )


def test_the_lumpsum_form_refuses_a_future_date(on_the_25th):
    form = _lumpsum(date(2026, 9, 26))
    assert not form.is_valid()
    assert "received_on" in form.errors


def test_the_lumpsum_form_takes_today_in_india(on_the_25th):
    assert _lumpsum(date(2026, 9, 25)).is_valid()
