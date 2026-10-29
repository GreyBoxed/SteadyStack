"""The real Kite client. The only implementation of `Broker` there is.

It runs in every environment, including a laptop, because Zerodha's static-IP
restriction covers order placement and not reads (M19c). Quotes, depth, the
instrument master, holdings and the order book are therefore live from the
first day of Phase 1, and the only call that waits for a registered address is
`place_order`.

Three things happen around every call, and none of them is optional:

  * a `BrokerCall` row is written -- request, response, latency, status, with
    credentials redacted -- because when our record and the broker's disagree,
    this table is the evidence (M44);
  * failures are translated into `broker.exceptions`, so callers decide what
    to do from a type rather than from a message;
  * reads retry, writes do not. A placement that times out may have reached
    the exchange; retrying it is how a day buys twice. The caller reconciles
    against the order book instead (D8).
"""

import logging
import time
from decimal import Decimal
from typing import Any

import requests
from django.conf import settings
from kiteconnect import KiteConnect
from kiteconnect import exceptions as kite_exceptions

from audit.context import current_trace_id
from audit.logging import fields
from audit.models import BrokerCall
from broker import mode
from broker.base import Broker
from broker.exceptions import (
    BrokerInputError,
    BrokerUnavailable,
    OrderRejected,
    RateLimited,
    SessionExpired,
    TransientBrokerError,
    UnknownBrokerError,
)
from broker.redaction import redact, scrub

logger = logging.getLogger("steadystack.broker")

#: Kite's own default is 7 seconds. A ladder tick has five minutes, but a call
#: that hangs for even one of them delays everything behind it.
DEFAULT_TIMEOUT_SECONDS = 7

#: Reads only. Two attempts after the first, spaced so a blip passes and a
#: real outage is not hammered.
READ_RETRIES = 2
RETRY_BACKOFF_SECONDS = (0.5, 2.0)

#: Every order this system places is a delivery trade on the regular variety.
VARIETY = "regular"
PRODUCT = "CNC"

#: Order types the exchange refuses without a market-protection percentage.
PROTECTED_ORDER_TYPES = frozenset({"MARKET", "SL-M"})


def _describe(exc: Exception) -> str:
    return f"{exc.__class__.__name__}: {exc}"


#: Kite does not use one exception class for a dead session. A quote or a
#: holdings call with an invalid or absent access token comes back as an
#: InputException reading "Invalid `api_key` or `access_token`", while an
#: expired token on other endpoints raises TokenException. Classifying the
#: first as a malformed request would mean a dead session is never detected,
#: the day is never parked (M7), and the operator is never asked to log in --
#: so the message is consulted as well as the class. Observed against the live
#: API, September 2026.
_DEAD_SESSION_MARKERS = ("access_token", "api_key")


def _reads_as_a_dead_session(exc: Exception) -> bool:
    message = str(exc).lower()
    if not any(marker in message for marker in _DEAD_SESSION_MARKERS):
        return False
    return any(word in message for word in ("invalid", "expired", "missing"))


#: requests' transport failures, as the Kite client surfaces them.
_REQUESTS_TRANSIENT = requests.exceptions.Timeout | requests.exceptions.ConnectionError


def translate(exc: Exception, *, call: str) -> Exception:
    """Map a Kite exception onto the taxonomy the caller reasons with.

    A dead session is the one worth naming: it has a human remedy and a
    deadline, and must never be confused with a transient network failure that
    a retry would fix, nor with a malformed request that a retry would repeat.
    """
    if isinstance(exc, kite_exceptions.TokenException):
        return SessionExpired(_describe(exc), call=call)
    if isinstance(
        exc, kite_exceptions.InputException | kite_exceptions.GeneralException
    ) and _reads_as_a_dead_session(exc):
        return SessionExpired(_describe(exc), call=call)
    if isinstance(exc, kite_exceptions.PermissionException):
        # Includes the rejection an unregistered address earns. The engine's
        # own gate should have refused first; reaching here means it did not.
        return BrokerInputError(_describe(exc), call=call)
    if isinstance(exc, kite_exceptions.OrderException):
        return OrderRejected(_describe(exc), call=call)
    if isinstance(exc, kite_exceptions.InputException):
        return BrokerInputError(_describe(exc), call=call)
    if isinstance(exc, kite_exceptions.NetworkException):
        # Kite raises this for 429 and 503 alike; the status is in the message
        # only, so treat it as retryable and let the caller's budget decide.
        return TransientBrokerError(_describe(exc), call=call)
    if isinstance(exc, kite_exceptions.DataException):
        return BrokerUnavailable(_describe(exc), call=call)
    if isinstance(exc, TimeoutError | ConnectionError | _REQUESTS_TRANSIENT):
        # The Kite client raises requests' own timeout and connection classes,
        # which are not the builtins: without them a dropped socket read as
        # unknown, never retried.
        return TransientBrokerError(_describe(exc), call=call)
    if isinstance(exc, kite_exceptions.GeneralException):
        return UnknownBrokerError(_describe(exc), call=call)
    return UnknownBrokerError(_describe(exc), call=call)


class KiteBroker(Broker):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        access_token: str | None = None,
        timeout: int | None = None,
        client: KiteConnect | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else settings.KITE_API_KEY
        self.api_secret = api_secret if api_secret is not None else settings.KITE_API_SECRET
        self._client = client or KiteConnect(
            api_key=self.api_key,
            access_token=access_token,
            timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
        )

    # -- call plumbing -----------------------------------------------------

    def _call(
        self,
        name: str,
        func,
        *,
        request: dict[str, Any] | None = None,
        retry: bool = False,
        **kwargs,
    ):
        """Run one broker call, persist it, and translate what it raises.

        `retry` is the caller's assertion that the call is idempotent. It is
        passed explicitly at every call site rather than inferred from the
        name, because the cost of guessing wrong is a duplicate order.
        """
        attempts = READ_RETRIES + 1 if retry else 1
        last: Exception | None = None

        for attempt in range(attempts):
            started = time.monotonic()
            try:
                response = func(**kwargs)
            except Exception as exc:  # translated below; never swallowed
                latency_ms = int((time.monotonic() - started) * 1000)
                translated = translate(exc, call=name)
                self._persist(
                    name,
                    request=request,
                    response=None,
                    latency_ms=latency_ms,
                    error=_describe(exc),
                )
                logger.warning(
                    "broker call failed",
                    extra=fields(
                        call=name,
                        attempt=attempt + 1,
                        error_type=translated.__class__.__name__,
                    ),
                )
                last = translated
                retryable = isinstance(translated, TransientBrokerError | RateLimited)
                if attempt + 1 < attempts and retryable:
                    time.sleep(RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)])
                    continue
                raise translated from exc

            latency_ms = int((time.monotonic() - started) * 1000)
            self._persist(name, request=request, response=response, latency_ms=latency_ms)
            return response

        raise last if last else UnknownBrokerError(call=name)

    def _persist(
        self,
        endpoint: str,
        *,
        request: dict[str, Any] | None,
        response: Any,
        latency_ms: int,
        error: str = "",
    ) -> None:
        """Write the call to the audit table.

        Redaction happens here, on the way in. Storing first and cleaning
        later would mean the token was written to disk, and the point of the
        deny-list is that it never is.
        """
        try:
            BrokerCall.objects.create(
                trace_id=current_trace_id(),
                endpoint=endpoint,
                method="POST" if endpoint.endswith((".place", ".session")) else "GET",
                request=redact(request),
                response=redact(_jsonable(response)),
                status_code=None,
                latency_ms=latency_ms,
                # Free text, so redacted by value: Kite's messages can quote
                # the key or token a call was made with.
                error=scrub(
                    error,
                    self.api_key,
                    self.api_secret,
                    getattr(self._client, "access_token", None),
                )[:2000],
            )
        except Exception:
            logger.exception("could not persist broker call", extra=fields(call=endpoint))

    # -- session -----------------------------------------------------------

    def login_url(self) -> str:
        return self._client.login_url()

    def create_session(self, request_token: str) -> dict[str, Any]:
        """Exchange a request token for an access token.

        The response carries the access token, so the redaction deny-list is
        the only thing standing between a daily login and a token on disk.
        """
        session = self._call(
            "session.generate",
            self._client.generate_session,
            request={"request_token": "[REDACTED]"},
            request_token=request_token,
            api_secret=self.api_secret,
        )
        self.set_access_token(session["access_token"])
        return session

    def set_access_token(self, access_token: str) -> None:
        self._client.set_access_token(access_token)

    def profile(self) -> dict[str, Any]:
        return self._call("user.profile", self._client.profile, retry=True)

    # -- market data -------------------------------------------------------

    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        return self._call(
            "market.instruments",
            self._client.instruments,
            request={"exchange": exchange},
            retry=True,
            exchange=exchange,
        )

    def quote(self, instruments: list[str]) -> dict[str, Any]:
        return self._call(
            "market.quote",
            lambda: self._client.quote(*instruments),
            request={"instruments": instruments},
            retry=True,
        )

    def depth(self, instrument: str) -> dict[str, Any]:
        """The order book for one instrument.

        Kite returns depth inside the full quote, so this is a projection
        rather than a separate endpoint -- but callers that need the book say
        so, and a future broker that separates them would not break them.
        """
        quotes = self.quote([instrument])
        quote = quotes.get(instrument)
        if quote is None:
            raise BrokerInputError(f"No quote for {instrument}", call="market.quote")
        return quote.get("depth", {})

    # -- account -----------------------------------------------------------

    def holdings(self) -> list[dict[str, Any]]:
        return self._call("portfolio.holdings", self._client.holdings, retry=True)

    def positions(self) -> dict[str, Any]:
        return self._call("portfolio.positions", self._client.positions, retry=True)

    def margins(self) -> dict[str, Any]:
        return self._call("user.margins", self._client.margins, retry=True)

    # -- orders ------------------------------------------------------------

    def place_order(
        self,
        *,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        price: Decimal | None = None,
        tag: str = "",
        market_protection: Decimal | None = None,
    ) -> str:
        """Place one order. The gate is checked here, before the call.

        Every other path into this method -- a management command, a shell, a
        future view -- passes through the same refusal, which is why it lives
        here and not in the caller.

        A market order must say how far from the last price it may fill: since
        1 April 2026 the exchange rejects one without `market_protection`
        (SEBI's retail algo rules). Refused here rather than sent, because the
        broker's rejection would arrive at 15:00 on the one order that has no
        later tick to try again.
        """
        mode.require_live_permitted()
        if order_type in PROTECTED_ORDER_TYPES and market_protection is None:
            raise ValueError(
                f"A {order_type} order needs market_protection; the exchange rejects "
                "one without it."
            )

        request = {
            "variety": VARIETY,
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "product": PRODUCT,
            "order_type": order_type,
            "price": str(price) if price is not None else None,
            "tag": tag or None,
            "market_protection": str(market_protection) if market_protection is not None else None,
        }

        return self._call(
            "order.place",
            self._client.place_order,
            request=request,
            retry=False,  # never: a timed-out placement may have reached the exchange
            variety=VARIETY,
            exchange=exchange,
            tradingsymbol=tradingsymbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=PRODUCT,
            order_type=order_type,
            price=float(price) if price is not None else None,
            validity="DAY",
            tag=tag or None,
            market_protection=float(market_protection) if market_protection is not None else None,
        )

    def cancel_order(self, order_id: str) -> str:
        return self._call(
            "order.cancel",
            self._client.cancel_order,
            request={"order_id": order_id},
            retry=False,
            variety=VARIETY,
            order_id=order_id,
        )

    def orders(self) -> list[dict[str, Any]]:
        return self._call("order.list", self._client.orders, retry=True)

    def order_history(self, order_id: str) -> list[dict[str, Any]]:
        return self._call(
            "order.history",
            self._client.order_history,
            request={"order_id": order_id},
            retry=True,
            order_id=order_id,
        )

    def trades(self) -> list[dict[str, Any]]:
        return self._call("order.trades", self._client.trades, retry=True)


def _jsonable(value: Any) -> Any:
    """Make a broker response storable in a JSONField.

    Kite parses its own date fields into datetimes, which json cannot encode.
    Truncating the stored copy would defeat the table's purpose, so values are
    stringified rather than dropped.
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
