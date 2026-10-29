"""JSON log lines.

The test that matters is the first one: a line without a trace ID is a line
that cannot be found again.
"""

import json
import logging
from datetime import date

from audit.context import bind_trading_day, reset_request_id, reset_trace_id, set_request_id
from audit.logging import JSONFormatter, fields


def record_with(extra: dict) -> logging.LogRecord:
    """A record carrying *extra*, built the way logging builds one."""
    record = logging.LogRecord(
        name="steadystack.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="refreshed",
        args=(),
        exc_info=None,
    )
    record.__dict__.update(extra)
    return record


def emit(level: int = logging.INFO, msg: str = "hello", **kwargs) -> dict:
    record = logging.LogRecord(
        name="steadystack.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=kwargs.pop("args", ()),
        exc_info=kwargs.pop("exc_info", None),
    )
    for key, value in kwargs.items():
        setattr(record, key, value)
    return json.loads(JSONFormatter().format(record))


def test_a_line_is_valid_json():
    assert emit()["message"] == "hello"


def test_every_line_carries_a_trace_id():
    token = bind_trading_day(date(2026, 9, 22))
    try:
        assert emit()["trace_id"] == "ts-2026-09-22"
    finally:
        reset_trace_id(token)


def test_the_level_and_logger_name_are_present():
    line = emit(level=logging.WARNING)
    assert line["level"] == "WARNING"
    assert line["logger"] == "steadystack.test"


def test_a_request_id_appears_when_one_is_bound():
    token = set_request_id("req123")
    try:
        assert emit()["request_id"] == "req123"
    finally:
        reset_request_id(token)


def test_no_request_id_key_outside_a_request():
    """A scheduled task has no request. An empty string would read as one."""
    assert "request_id" not in emit()


def test_extras_are_merged_rather_than_stringified():
    line = emit(tick_no=3, symbol="INFY")
    assert line["tick_no"] == 3
    assert line["symbol"] == "INFY"


def test_message_arguments_are_interpolated():
    line = emit(msg="placed %s at %s", args=("INFY", "1650.50"))
    assert line["message"] == "placed INFY at 1650.50"


def test_an_exception_is_rendered_into_the_line():
    try:
        raise ValueError("no depth for token 408065")
    except ValueError:
        import sys

        line = emit(level=logging.ERROR, msg="quote failed", exc_info=sys.exc_info())

    assert "no depth for token 408065" in line["exception"]
    assert line["message"] == "quote failed"


def test_an_unserialisable_extra_does_not_lose_the_line():
    """A logging call must never raise inside the code it is describing."""

    class Opaque:
        def __repr__(self) -> str:
            return "<Opaque>"

    line = emit(thing=Opaque())
    assert line["thing"] == "<Opaque>"
    assert line["message"] == "hello"


def test_structured_values_are_merged_into_the_line():
    line = json.loads(JSONFormatter().format(record_with(fields(tick_no=3, symbol="INFY"))))
    assert line["tick_no"] == 3
    assert line["symbol"] == "INFY"


def test_a_domain_word_that_collides_with_a_logrecord_attribute_survives():
    """`created`, `name`, `module` and `args` are LogRecord's own attributes,
    and passing one through `extra=` raises inside the logging call -- in the
    middle of the code being logged. These are ordinary words here: a refresh
    reports how many instruments it *created*, by *name*."""
    logger = logging.getLogger("steadystack.test.collision")
    logger.info("refreshed", extra=fields(created=12, name="INFY", module="portfolio"))

    line = json.loads(JSONFormatter().format(record_with(fields(created=12, name="INFY"))))
    assert line["created"] == 12
    assert line["name"] == "INFY"


def test_the_carrier_attribute_itself_is_not_emitted():
    line = json.loads(JSONFormatter().format(record_with(fields(tick_no=3))))
    assert not any(key.startswith("steadystack_") for key in line)
