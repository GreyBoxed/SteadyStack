"""Bind trace and request identity for the duration of a request.

Outermost in the middleware chain, so that a request rejected by the OTP gate
still produces traceable log lines. A rejected request is exactly the one
somebody will come looking for.
"""

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from audit.context import (
    bind_trading_day,
    new_request_id,
    reset_request_id,
    reset_trace_id,
    set_request_id,
    today_ist,
)


class RequestContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = new_request_id()
        # Generated here rather than trusted from a header: an inbound
        # X-Request-ID is caller-controlled, and these IDs end up in an
        # append-only table.
        request_token = set_request_id(request_id)
        trace_token = bind_trading_day(today_ist())
        request.request_id = request_id

        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            return response
        finally:
            # Reset even when the view raised: worker threads are reused, and
            # a leaked ID would mislabel the next request handled by this one.
            reset_trace_id(trace_token)
            reset_request_id(request_token)
