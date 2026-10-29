"""What a trade costs beyond the price of the shares.

There is exactly one cost number in this system (D6):

    cost = gross + brokerage + exchange charges + GST + stamp duty + STT

It drives the cash ledger, carry, lots, realised gain and unrealised gain
alike. Nothing is carved out of it -- every charge on the trade is part of
what the shares cost (M35).

**The rates here are taken from the broker's published schedule**
(zerodha.com/charges, equity delivery, read 22 September 2026) and were
confirmed against a real contract note by the operator on the same day. They
are values in code rather than constants in a formula because every one of
them moves: STT and stamp duty with a Union Budget, exchange charges with a
circular, GST with a rate change, the depository fee whenever CDSL revises it.
When one does move, the tests that pin these figures fail, which is how the
change gets noticed rather than quietly absorbed into the basis.

That is also why a lot written from these rates is marked `estimated` rather
than `final`. The contract note is what settles it (M35b): the difference is
posted as a cash-ledger adjustment and the lot moves to `final`. An estimate
lets the evening settle; the note makes it true.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

PAISE = Decimal("0.01")
ZERO = Decimal("0.00")


def paise(value: Decimal) -> Decimal:
    return Decimal(value).quantize(PAISE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RateCard:
    """Charge rates, as fractions of turnover unless stated otherwise.

    Equity delivery. The defaults are the published retail rates; they are
    values rather than constants because they change, and they are checked
    against a contract note rather than trusted.
    """

    #: Zerodha charges nothing for delivery equity. A broker that does would
    #: set this rather than have the arithmetic ignore it.
    brokerage_pct: Decimal = ZERO
    brokerage_cap: Decimal | None = None

    #: 0.1% on buy and on sell.
    securities_transaction_tax_pct: Decimal = Decimal("0.001")

    #: Per exchange, because they differ and an instrument knows which it is.
    #: NSE 0.00307%, BSE 0.00375%.
    exchange_txn_pct: Decimal = Decimal("0.0000307")
    bse_exchange_txn_pct: Decimal = Decimal("0.0000375")

    #: Rs 10 per crore.
    sebi_turnover_pct: Decimal = Decimal("0.000001")

    #: 0.015%, or Rs 1500 per crore, on the buy side only.
    stamp_duty_pct: Decimal = Decimal("0.00015")

    #: On brokerage + SEBI charges + transaction charges. Never on the taxes.
    gst_pct: Decimal = Decimal("0.18")

    #: Per scrip on a sell, flat: Rs 3.50 CDSL + Rs 9.50 broker + Rs 2.34 GST.
    #: The GST is already inside this figure, which is why it is not taxed
    #: again alongside the other service charges.
    dp_charge: Decimal = Decimal("15.34")

    def transaction_pct_for(self, exchange: str) -> Decimal:
        return self.bse_exchange_txn_pct if exchange.upper() == "BSE" else self.exchange_txn_pct


DEFAULT_RATES = RateCard()


@dataclass(frozen=True)
class Charges:
    """Every component, kept separately (S5).

    Stored individually rather than as one total: a basis that can be taken
    apart is one that can be checked against a contract note line by line, and
    the components stay available if they are ever needed elsewhere.
    """

    brokerage: Decimal = ZERO
    securities_transaction_tax: Decimal = ZERO
    exchange_txn: Decimal = ZERO
    sebi_turnover: Decimal = ZERO
    stamp_duty: Decimal = ZERO
    gst: Decimal = ZERO
    dp_charge: Decimal = ZERO

    @property
    def total(self) -> Decimal:
        return paise(
            self.brokerage
            + self.securities_transaction_tax
            + self.exchange_txn
            + self.sebi_turnover
            + self.stamp_duty
            + self.gst
            + self.dp_charge
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "brokerage": str(self.brokerage),
            "securities_transaction_tax": str(self.securities_transaction_tax),
            "exchange_txn": str(self.exchange_txn),
            "sebi_turnover": str(self.sebi_turnover),
            "stamp_duty": str(self.stamp_duty),
            "gst": str(self.gst),
            "dp_charge": str(self.dp_charge),
            "total": str(self.total),
        }


def estimate(
    *,
    turnover: Decimal,
    side: str,
    rates: RateCard | None = None,
    exchange: str = "NSE",
    dp_charged: bool = False,
) -> Charges:
    """Estimate the charges on one fill.

    Buy and sell differ in two places and only two: stamp duty is charged on
    the buy, and the depository charge on the sell.

    Every component here is a rate on turnover and therefore divides cleanly
    across a sale that fills in pieces -- except the depository charge, which
    is a flat fee per scrip per *day* however many times the order filled.
    The sell ladder fills in pieces by design, so `dp_charged` says that some
    earlier fill of this scrip today has already carried it and this one must
    not carry it again.
    """
    rates = rates or DEFAULT_RATES
    turnover = Decimal(turnover)
    if turnover <= 0:
        return Charges()

    is_buy = side.upper() == "BUY"

    brokerage = paise(turnover * rates.brokerage_pct)
    if rates.brokerage_cap is not None:
        brokerage = min(brokerage, rates.brokerage_cap)

    exchange_txn = paise(turnover * rates.transaction_pct_for(exchange))
    sebi = paise(turnover * rates.sebi_turnover_pct)

    return Charges(
        brokerage=brokerage,
        securities_transaction_tax=paise(turnover * rates.securities_transaction_tax_pct),
        exchange_txn=exchange_txn,
        sebi_turnover=sebi,
        # GST falls on the service charges, never on the tax components.
        gst=paise((brokerage + exchange_txn + sebi) * rates.gst_pct),
        stamp_duty=paise(turnover * rates.stamp_duty_pct) if is_buy else ZERO,
        dp_charge=ZERO if (is_buy or dp_charged) else rates.dp_charge,
    )


def cost_of(
    *,
    quantity: int,
    price: Decimal,
    side: str = "BUY",
    exchange: str = "NSE",
    dp_charged: bool = False,
) -> tuple[Decimal, Charges]:
    """The one definition: gross plus every charge (D6)."""
    gross = paise(Decimal(price) * quantity)
    return gross, estimate(turnover=gross, side=side, exchange=exchange, dp_charged=dp_charged)
