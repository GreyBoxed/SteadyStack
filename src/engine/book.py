"""What the portfolio holds, as far as ranking is concerned.

A live day and a paper day own different things. Live, the broker's holdings
are the portfolio, and `Holding` is synced from them each morning. Paper, the
broker's holdings are the real account -- which a paper order never touches --
and the portfolio is the paper book: the lots every paper fill created
(M19c). Ranking a paper day against the real account ranks it against a book
that is empty every morning, and it never converges.

`Holding` is left as the broker's record in both modes. The divergence check
(M42) compares it with the lots, and that only means something while they are
two records rather than one copied into the other.
"""

from django.db.models import Sum

from configuration.store import current as current_configuration
from orders.models import OrderMode
from portfolio.lots import in_book
from portfolio.models import Holding, Lot


def book_for_ranking(symbols) -> dict[str, int]:
    """Shares held of each of *symbols*, from the book this mode trades."""
    symbols = list(symbols)

    if current_configuration().is_live:
        return dict(
            Holding.objects.filter(instrument_id__in=symbols).values_list(
                "instrument_id", "quantity"
            )
        )

    # The paper book's open lots only (M-1): a name held in opening or live
    # lots is not held by the paper engine, which could not sell it either.
    lots = Lot.objects.filter(
        instrument_id__in=symbols, remaining_quantity__gt=0, superseded_by__isnull=True
    )
    return dict(
        in_book(lots, OrderMode.PAPER)
        .values("instrument_id")
        .annotate(held=Sum("remaining_quantity"))
        .values_list("instrument_id", "held")
    )
