"""The broker failure taxonomy.

Phase 2's ladder has to decide, without reading a message string, whether to
retry this tick, park the day, back off, or stop and alert. Kite's own
exceptions do not carve the space that way, so every call is translated into
one of these on the way out of `broker/kite.py`.

The categories are chosen by what the caller must *do*, not by what went
wrong:

  * `TransientBrokerError`  -- try again shortly; nothing is known to have happened
  * `RateLimited`           -- back off for a stated interval
  * `SessionExpired`        -- park the day and ask the operator to log in
  * `BrokerInputError`      -- our request was wrong; retrying repeats the mistake
  * `BrokerUnavailable`     -- the venue or the broker is down
  * `OrderRejected`         -- the broker understood and declined
  * `UnknownBrokerError`    -- unclassified, and therefore never retried

`BrokerCallFailed` is deliberately not a category: everything here is one,
and a bare `except BrokerError` is the escape hatch for code that genuinely
does not care which.
"""


class BrokerError(Exception):
    """Base for every failure raised out of the broker package."""

    def __init__(self, message: str = "", *, call: str = "", detail: object = None) -> None:
        super().__init__(message or self.__class__.__name__)
        self.call = call
        self.detail = detail


class TransientBrokerError(BrokerError):
    """A timeout or a connection reset. Safe to retry an idempotent read.

    A placement that fails this way is *not* safe to retry blindly: the order
    may have reached the exchange. The caller reconciles against the broker's
    order book instead (D8).
    """


class RateLimited(TransientBrokerError):
    """Too many requests. `retry_after` is seconds, when the broker says."""

    def __init__(self, message: str = "", *, retry_after: float | None = None, **kwargs) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class SessionExpired(BrokerError):
    """The access token is dead. Nothing works again until the operator logs in.

    Distinct from every other failure because it has a human remedy and a
    deadline: the day is parked, not failed (M7).
    """


class BrokerInputError(BrokerError):
    """The broker rejected the request as malformed or impossible.

    A wrong tick size, an unknown symbol, a quantity of zero. Retrying sends
    the same wrong request again, so callers must not.
    """


class BrokerUnavailable(BrokerError):
    """The broker or the exchange is down, or the instrument is not tradable."""


class OrderRejected(BrokerError):
    """The order was understood and declined -- margin, circuit limit, ban."""


class UnknownBrokerError(BrokerError):
    """Unclassified. Never retried automatically, always alerted."""


class LiveOrdersRefused(BrokerError):
    """The engine refused to place a live order before making any call.

    Raised when the mode is live but the conditions for live are not met
    (M12a, M19b). It is not a broker failure -- the broker was never asked --
    but it surfaces where callers already handle broker errors, so no
    placement path can forget about it.
    """
