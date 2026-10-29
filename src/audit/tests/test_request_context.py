"""Request-scoped identity.

The leak case is the one worth testing: worker threads are reused, so an ID
left bound after a response would label the next request with the previous
one's identity, and the audit trail would be quietly wrong.
"""

from datetime import date

import time_machine
from django.http import HttpResponse
from django.test import RequestFactory

from audit.context import current_request_id, current_trace_id
from audit.middleware import RequestContextMiddleware


def run(view=None):
    seen = {}

    def default_view(request):
        seen["request_id"] = current_request_id()
        seen["trace_id"] = current_trace_id()
        seen["attribute"] = getattr(request, "request_id", None)
        return HttpResponse("ok")

    request = RequestFactory().get("/portfolio/")
    response = RequestContextMiddleware(view or default_view)(request)
    return response, seen


def test_a_request_id_is_bound_for_the_view():
    _response, seen = run()
    assert seen["request_id"]


def test_the_request_id_is_also_on_the_request_object():
    _response, seen = run()
    assert seen["attribute"] == seen["request_id"]


def test_the_request_id_is_returned_in_a_header():
    """So a user reporting "it failed at 10:04" can be matched to a log line."""
    response, seen = run()
    assert response["X-Request-ID"] == seen["request_id"]


@time_machine.travel("2026-09-22 09:45:00+05:30")
def test_the_trace_id_is_todays_trading_day():
    _response, seen = run()
    assert seen["trace_id"] == "ts-2026-09-22"


def test_two_requests_get_different_ids():
    _r1, first = run()
    _r2, second = run()
    assert first["request_id"] != second["request_id"]


def test_the_request_id_does_not_outlive_the_request():
    run()
    assert current_request_id() == ""


@time_machine.travel("2026-09-22 09:45:00+05:30")
def test_identity_is_released_even_when_the_view_raises():
    """A view that raises must not leave its IDs bound to the worker thread."""

    def exploding_view(_request):
        raise RuntimeError("boom")

    try:
        RequestContextMiddleware(exploding_view)(RequestFactory().get("/portfolio/"))
    except RuntimeError:
        pass

    assert current_request_id() == ""
    # Falls back to today's rather than retaining the request's binding.
    assert current_trace_id() == f"ts-{date(2026, 9, 22):%Y-%m-%d}"
