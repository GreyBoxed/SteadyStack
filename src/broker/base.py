"""The only vocabulary the rest of the system has for talking to a broker.

Two things this interface deliberately cannot express:

  * **modify.** Orders are never modified (M21). The ladder places a new
    order at each tick and leaves earlier ones resting. An interface without
    a `modify` cannot quietly grow a reprice back into the system a year from
    now, when the reason has been forgotten.
  * **a simulator.** There is one implementation, the real Kite client, used
    in every environment. Whether an order is *sent* is decided by mode
    (M19a-M19c), not by swapping the client -- so nothing downstream is ever
    written against a fake that behaves better than the real thing.

The types are plain dicts rather than models on purpose: this package is the
boundary, and a broker payload that becomes a domain object here would make
the domain depend on the broker's shape.
"""

from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any


class Broker(ABC):
    # -- session ----------------------------------------------------------

    @abstractmethod
    def login_url(self) -> str:
        """The URL that begins the operator's daily login."""

    @abstractmethod
    def create_session(self, request_token: str) -> dict[str, Any]:
        """Exchange a request token for an access token.

        The response body contains the access token, which is why redaction
        exists and is tested (`broker/redaction.py`).
        """

    @abstractmethod
    def set_access_token(self, access_token: str) -> None:
        """Use *access_token* for subsequent calls."""

    @abstractmethod
    def profile(self) -> dict[str, Any]:
        """The account behind the current session. Used to prove it is alive."""

    # -- market data (works in every environment: M19c) --------------------

    @abstractmethod
    def instruments(self, exchange: str | None = None) -> list[dict[str, Any]]:
        """The instrument master, including each instrument's tick size (M11b)."""

    @abstractmethod
    def quote(self, instruments: list[str]) -> dict[str, Any]:
        """Full quotes, depth included, keyed by `EXCHANGE:TRADINGSYMBOL`."""

    @abstractmethod
    def depth(self, instrument: str) -> dict[str, Any]:
        """The order book for one instrument: bids and asks.

        Named separately from `quote` because selection and pricing care about
        the book specifically -- affordability is tested against the ask, not
        the last traded price (M15).
        """

    # -- account -----------------------------------------------------------

    @abstractmethod
    def holdings(self) -> list[dict[str, Any]]:
        """Settled holdings. Averages only; the broker does not return lots."""

    @abstractmethod
    def positions(self) -> dict[str, Any]:
        """Day and net positions."""

    @abstractmethod
    def margins(self) -> dict[str, Any]:
        """Available funds."""

    # -- orders ------------------------------------------------------------

    @abstractmethod
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
        """Place one order and return the broker's order id.

        The only call in this interface that is gated by mode and by the
        registered address (M12a, M19b). A market order carries
        `market_protection`, the percentage from the last price it may fill
        within; the exchange rejects one without it.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> str:
        """Cancel one order and return its id."""

    @abstractmethod
    def orders(self) -> list[dict[str, Any]]:
        """Today's order book, as the broker sees it.

        Kite is authoritative for order state (D8): a postback is a trigger to
        come and read this, never a payload to trust on its own.
        """

    @abstractmethod
    def order_history(self, order_id: str) -> list[dict[str, Any]]:
        """Every state one order has passed through."""

    @abstractmethod
    def trades(self) -> list[dict[str, Any]]:
        """Today's fills."""
