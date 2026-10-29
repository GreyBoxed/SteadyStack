"""Fill a local database with a plausible history, through the real code.

Every row here is written the way the system writes it: a fill becomes a lot
through `portfolio.lots.from_fill` and a journal entry through
`journal.trades.post_fill`, a day is allocated its budget and summarised into a
ledger row through `engine.ledger.settle_day`, a sale consumes lots through
`gains.fifo.consume`.
Nothing is inserted behind the engine's back, so a screen that renders from
this is rendering from data the engine could actually have produced -- which
is the only kind worth looking at.

Development only. It refuses unless DEBUG is on and the mode is paper, because
a command that invents fills has no business anywhere near a live account.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from audit.context import today_ist, trace_id_for

# Symbol, target %, opening price, drift per day, token.
UNIVERSE = [
    ("HDFCBANK", "14.00", "1528.65", "0.9", 341249),
    ("INFY", "12.00", "1776.40", "1.1", 408065),
    ("RELIANCE", "12.00", "1402.10", "0.8", 738561),
    ("TCS", "10.00", "3482.90", "1.7", 2953217),
    ("ICICIBANK", "10.00", "1271.20", "0.7", 1270529),
    ("ITC", "9.00", "448.55", "0.2", 424961),
    ("LT", "9.00", "3745.80", "1.6", 2939649),
    ("ASIANPAINT", "8.00", "2590.40", "-0.9", 60417),
    ("SUNPHARMA", "8.00", "1684.30", "0.9", 857857),
    ("TITAN", "8.00", "3419.95", "1.2", 897537),
]

DAILY_AMOUNT = Decimal("2600.00")
BENCHMARK_OPEN = Decimal("24000")


class Command(BaseCommand):
    help = "Populate a development database with a believable trading history."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--days", type=int, default=250, help="Trading days to simulate.")
        parser.add_argument("--reset", action="store_true", help="Delete what is there first.")

    def handle(self, *args, **options) -> None:
        self._refuse_unless_safe()

        if options["reset"]:
            self._wipe()

        from engine.models import TradeSheet

        if TradeSheet.objects.exists():
            raise CommandError("There is already data here. Pass --reset to replace it.")

        self._configure()
        instruments = self._instruments()
        portfolio = self._model_portfolio(instruments)
        days = self._trading_days(options["days"])

        self._lumpsum(days[0])
        self._run_days(days, instruments, portfolio)
        self._divergence(days[-1], instruments)
        self._broker_holdings(instruments)

        self.stdout.write(
            self.style.SUCCESS(
                f"Seeded {len(days)} trading days, {len(instruments)} instruments. "
                f"Sign in and look at /holdings/."
            )
        )

    # -- guards ----------------------------------------------------------

    def _refuse_unless_safe(self) -> None:
        from django.db.utils import ProgrammingError

        from broker.mode import is_paper

        if not settings.DEBUG:
            raise CommandError("seed_demo runs only with DEBUG on. This is a development tool.")

        try:
            paper = is_paper()
        except ProgrammingError:
            # No configuration table means nothing has been configured, which
            # is paper. Reached when --reset left the schema empty.
            paper = True

        if not paper:
            raise CommandError(
                "The configured mode is live. A command that invents fills does not "
                "run against a live account, even in a paper-shaped database."
            )

    def _wipe(self) -> None:
        """Recreate the schema, because the data cannot simply be deleted.

        The append-only tables carry a trigger that refuses DELETE and UPDATE
        from every role, schema owner included -- that is what makes the
        invariant a property of the database rather than a promise. So a
        re-seed is not a delete: it is a fresh schema and a fresh migrate,
        which is also the only honest way to get a clean ledger.
        """
        from django.core.management import call_command
        from django.db import connections
        from psycopg import sql

        self.stdout.write(
            "Recreating the schema; the append-only tables cannot be cleared any other way..."
        )

        # The owner's alias exists only when DATABASE_MIGRATION_URL is set;
        # without it the one connection there is owns the schema as well.
        alias = "migration" if "migration" in settings.DATABASES else "default"
        runtime = settings.DATABASES["default"]["USER"]
        owner = settings.DATABASES[alias]["USER"]

        # Release every session first: an open connection holds locks the DROP
        # would wait on, and query plans that would outlive the tables.
        connections.close_all()

        connections[alias].set_autocommit(False)
        with connections[alias].cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

            # DROP SCHEMA takes the grants with it, and the runtime role is a
            # different role from the owner -- without these it can see the
            # new tables no better than a stranger could. The same three
            # statements live in ops/postgres/init/01-roles.sql, which runs
            # only when the container is first created.
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(runtime))
            )
            cursor.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                ).format(sql.Identifier(owner), sql.Identifier(runtime))
            )
            cursor.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public "
                    "GRANT USAGE, SELECT ON SEQUENCES TO {}"
                ).format(sql.Identifier(owner), sql.Identifier(runtime))
            )

        # The grants must be committed before migrate creates the tables they
        # are meant to cover, or the default privileges never reach them.
        connections[alias].commit()
        connections[alias].set_autocommit(True)

        call_command("migrate", verbosity=0)
        connections.close_all()

    # -- the world -------------------------------------------------------

    def _configure(self) -> None:
        """Set the daily amount through the audited path, not behind it.

        The budget every day spends comes from configuration, so seeding it
        any other way would leave the history disagreeing with the settings
        screen that claims to explain it.
        """
        from configuration.store import update

        update(
            changed_by="operator",
            reason="seeded for local testing",
            daily_amount=DAILY_AMOUNT,
        )

    def _instruments(self) -> dict:
        from portfolio.models import Instrument

        made = {}
        for symbol, _, _, _, token in UNIVERSE:
            made[symbol] = Instrument.objects.create(
                tradingsymbol=symbol,
                exchange="NSE",
                instrument_token=token,
                name=symbol.title(),
                tick_size=Decimal("0.05"),
            )
        return made

    def _model_portfolio(self, instruments: dict):
        from model_portfolio.models import ModelPortfolio, ModelPortfolioRow

        portfolio = ModelPortfolio.objects.create(
            version=1,
            uploaded_by="operator",
            source_filename="model-portfolio-v1.csv",
            content_hash="seed" + "0" * 60,
            note="Seeded for local testing.",
        )
        for symbol, target, _, _, _ in UNIVERSE:
            ModelPortfolioRow.objects.create(
                portfolio=portfolio,
                instrument=instruments[symbol],
                target_pct=Decimal(target),
            )
        return portfolio

    def _trading_days(self, count: int) -> list[date]:
        from trading_calendar.sessions import is_trading_day

        days, day = [], today_ist()
        while len(days) < count:
            if is_trading_day(day):
                days.append(day)
            day -= timedelta(days=1)
        return list(reversed(days))

    def _price_on(self, symbol: str, index: int) -> Decimal:
        """A gently drifting price, deterministic so screenshots are stable."""
        for sym, _, opening, drift, _ in UNIVERSE:
            if sym == symbol:
                wobble = Decimal(str((index * 7919 % 23) - 11)) / Decimal("10")
                return (Decimal(opening) + Decimal(drift) * index + wobble).quantize(
                    Decimal("0.05")
                )
        raise KeyError(symbol)

    def _book(self, ask: Decimal, *, seed: int) -> dict:
        """Five levels a side around a one-tick spread, as Kite returns them.

        Deterministic, so a screenshot of the depth ladder is stable.
        """
        step = Decimal("0.05")

        def size(level: int, side: int) -> int:
            return 400 + (seed * 7919 + level * 104729 + side * 1299709) % 21000

        return {
            "buy": [
                {
                    "price": float(ask - step * (level + 1)),
                    "quantity": size(level, 0),
                    "orders": 3 + size(level, 0) % 70,
                }
                for level in range(5)
            ],
            "sell": [
                {
                    "price": float(ask + step * level),
                    "quantity": size(level, 1),
                    "orders": 3 + size(level, 1) % 70,
                }
                for level in range(5)
            ],
        }

    def _rank(self, prices: dict, budget: Decimal):
        """Rank and walk exactly as the engine does, against a seeded book."""
        from engine.selection import Quote, rank, select
        from gains.fifo import available
        from journal.models import Book

        ranking = rank(
            weights={s: Decimal(t) for s, t, *_ in UNIVERSE},
            holdings={s: available(s, book=Book.PAPER) for s, *_ in UNIVERSE},
            quotes={s: Quote(tradingsymbol=s, last_price=prices[s], ask=prices[s]) for s in prices},
        )
        return ranking, select(ranking, spendable_cash=budget)

    # -- the days --------------------------------------------------------

    def _lumpsum(self, day: date) -> None:
        from journal.cash import record_lumpsum

        record_lumpsum(
            Decimal("25000.00"),
            received_on=day,
            actor="operator",
            reference="birthday, from the grandparents",
        )

    @transaction.atomic
    def _run_days(self, days: list[date], instruments: dict, portfolio) -> dict:
        from engine.ledger import settle_day, spendable_on
        from engine.models import SkipReason, TradeSheet, TradeSheetLine, TradeSheetState
        from journal.cash import allocate_daily
        from journal.models import Book
        from journal.trades import post_fill, settle_trades
        from orders.models import (
            Fill,
            NoPlaceReason,
            Order,
            OrderMode,
            OrderState,
            OrderType,
            Tick,
        )
        from performance import valuation
        from portfolio import lots as lot_writer

        last_prices: dict[str, Decimal] = {}

        for index, day in enumerate(days):
            prices = {s: self._price_on(s, index) for s, *_ in UNIVERSE}
            last_prices = prices

            allocate_daily(day, book=Book.PAPER)
            budget = spendable_on(day)

            # The engine's own ranking and walk, so the history accumulates the
            # way a real one does and the stored ranking reads like a real day's.
            ranking, selection = self._rank(prices, budget)
            picks = [p for p in selection.picks if p.quantity]
            lead = picks[0].candidate.tradingsymbol if picks else None

            skipped = index == len(days) - 6 or not picks
            state = TradeSheetState.SKIPPED if skipped else TradeSheetState.FILLED

            sheet = TradeSheet.objects.create(
                trading_date=day,
                version=1,
                generated_by="system",
                trace_id=trace_id_for(day),
                state=state,
                skip_reason=SkipReason.NO_SESSION_BY_CUTOFF if skipped else "",
                model_portfolio=portfolio,
                instrument=None if skipped else instruments[lead],
                carry_in=budget - DAILY_AMOUNT if budget > DAILY_AMOUNT else Decimal("0"),
                spendable_cash=budget,
                quantity=0 if skipped else selection.quantity,
                reference_price=prices[lead] if lead else Decimal("0"),
                # A seeded day traded, so somebody authorized it (M15f).
                authorized_at=None if skipped else timezone.now(),
                authorized_by="" if skipped else "operator",
                ranking_snapshot={
                    **ranking.as_snapshot(),
                    "passed_over": {
                        "unaffordable": list(selection.unaffordable),
                        "capped": list(selection.capped),
                    },
                },
            )

            if skipped:
                Tick.objects.create(
                    tradesheet=sheet,
                    trading_date=day,
                    tick_no=0,
                    trace_id=trace_id_for(day),
                    no_place_reason=NoPlaceReason.NO_SESSION,
                    detail="no Kite session at 09:45; the day parked and then skipped",
                )
                settle_day(day, book=Book.PAPER, note="skipped: no session before the cutoff")
                valuation.snapshot(
                    day,
                    prices=prices,
                    benchmark_level=BENCHMARK_OPEN + Decimal(index) * Decimal("11.5"),
                    book=Book.PAPER,
                )
                continue

            # Every name the day buys, one share a tick each, as the ladder does.
            opens = timezone.make_aware(datetime.combine(day, time(9, 45)))
            for rank_no, pick in enumerate(picks, start=1):
                symbol = pick.candidate.tradingsymbol
                price = prices[symbol]
                instrument = instruments[symbol]
                TradeSheetLine.objects.create(
                    tradesheet=sheet,
                    instrument=instrument,
                    rank=rank_no,
                    quantity=pick.quantity,
                    reference_price=price,
                )
                for slice_no in range(pick.quantity):
                    placed_at = opens + timedelta(minutes=5 * slice_no, seconds=1 + rank_no % 3)
                    order = Order.objects.create(
                        tag=f"tx-{day:%y%m%d}-{symbol}-{slice_no}"[:32],
                        tradesheet=sheet,
                        instrument=instrument,
                        trading_date=day,
                        tick_no=slice_no,
                        mode=OrderMode.PAPER,
                        order_type=OrderType.LIMIT,
                        quantity=1,
                        filled_quantity=1,
                        limit_price=price,
                        state=OrderState.COMPLETE,
                        trace_id=trace_id_for(day),
                    )
                    tick = Tick.objects.create(
                        tradesheet=sheet,
                        trading_date=day,
                        tick_no=slice_no,
                        instrument=instrument,
                        trace_id=trace_id_for(day),
                        best_bid=price - Decimal("0.05"),
                        best_ask=price,
                        depth_snapshot=self._book(price, seed=index * 31 + slice_no + rank_no),
                        slice_qty=1,
                        committed_before=slice_no,
                        order=order,
                    )
                    # auto_now_add stamps the seeding moment; the day happened earlier.
                    Order.objects.filter(pk=order.pk).update(placed_at=placed_at)
                    Tick.objects.filter(pk=tick.pk).update(fired_at=placed_at)
                    # No stored charges: the rate card prices them, as it
                    # does for every production fill (m-4). A custom figure
                    # here would part the journal from the lots' basis.
                    fill = Fill.objects.create(
                        order=order,
                        kite_trade_id=f"paper-{day:%y%m%d}-{symbol}-{slice_no}",
                        quantity=1,
                        price=price,
                        executed_at=placed_at + timedelta(minutes=3 + (index + slice_no) % 17),
                        trace_id=trace_id_for(day),
                    )
                    lot_writer.from_fill(fill)
                    post_fill(fill)

            # The sale happens on its day, before that day's row is written:
            # its batch reinvests the proceeds on that day, and the row
            # carries them in its proceeds column.
            if day == days[-2]:
                self._rebalance_sell(day, prices)
            settle_trades(day, book=Book.PAPER)
            settle_day(day, book=Book.PAPER)
            valuation.snapshot(
                day,
                prices=prices,
                benchmark_level=BENCHMARK_OPEN + Decimal(index) * Decimal("11.5"),
                book=Book.PAPER,
            )

        return last_prices

    # -- the exceptions worth showing ------------------------------------

    def _rebalance_sell(self, day: date, prices: dict) -> None:
        """One sale, through the shape a rebalance batch actually produces.

        A sell order and its fill, then FIFO consumes the oldest lots against
        that fill -- so realised gains, the share ledger's `rebalance_sell`
        source and the lot chain all agree, as they would in a real batch.
        """
        from django.utils import timezone

        from engine.models import TradeSheet, TradeSheetState
        from gains.fifo import available, consume
        from journal.models import Book
        from journal.trades import post_fill, reinvest
        from orders.models import (
            Fill,
            Order,
            OrderMode,
            OrderSide,
            OrderState,
            OrderType,
        )

        # A rebalance batch is its own stream, so it gets its own sheet rather
        # than hanging a sell off the day's buy -- which made that day look
        # like it had filled more than it bought.
        sheet, _ = TradeSheet.objects.get_or_create(
            trading_date=day,
            version=2,
            defaults={
                "trace_id": trace_id_for(day),
                "state": TradeSheetState.FILLED,
                "generated_by": "operator",
            },
        )

        from datetime import datetime, time

        from rebalance.batches import names_bought_today
        from rebalance.models import BatchState, RebalanceSell, RowState

        # Never a name the day bought (D-S2): CNC nets a same-day buy and sell.
        bought = names_bought_today(day) | set(
            Fill.objects.filter(order__trading_date=day, order__side=OrderSide.BUY).values_list(
                "order__instrument_id", flat=True
            )
        )
        sold_at = timezone.make_aware(datetime.combine(day, time(13, 10)))

        for symbol in ("ITC", "HDFCBANK", "INFY", "TCS", "RELIANCE", "BHARTIARTL"):
            held = available(symbol, book=Book.PAPER)
            if held < 2 or symbol in bought:
                continue

            price = prices[symbol]
            stream = f"rebalance-{day:%Y%m%d}"
            batch = RebalanceSell.objects.create(
                uploaded_by="operator",
                source_filename="rebalance.csv",
                content_hash=f"seed-{day:%Y%m%d}",
                state=BatchState.COMPLETE,
                trading_date=day,
                stream=stream,
                reinvest=True,
                started_at=sold_at,
                finished_at=sold_at,
                trace_id=trace_id_for(day),
            )
            order = Order.objects.create(
                tag=f"rb-{day:%y%m%d}-{symbol}",
                tradesheet=sheet,
                instrument_id=symbol,
                trading_date=day,
                stream=stream,
                tick_no=0,
                side=OrderSide.SELL,
                mode=OrderMode.PAPER,
                order_type=OrderType.LIMIT,
                quantity=2,
                filled_quantity=2,
                limit_price=price,
                state=OrderState.COMPLETE,
                trace_id=trace_id_for(day),
            )
            fill = Fill.objects.create(
                order=order,
                kite_trade_id=f"paper-rb-{day:%y%m%d}",
                quantity=2,
                price=price,
                charges=Decimal("3.40"),
                executed_at=sold_at,
                trace_id=trace_id_for(day),
            )
            consumption = consume(
                instrument_id=symbol,
                quantity=2,
                proceeds_per_unit=price,
                disposed_on=day,
                fill=fill,
                charges=Decimal("3.40"),
            )
            post_fill(fill, consumption=consumption)
            batch.rows.create(
                instrument_id=symbol,
                quantity=2,
                sold_quantity=2,
                validation_state=RowState.FILLED,
                line=1,
            )
            # The batch closes and gives its net proceeds to the engine, as
            # `rebalance.ladder.close_batch` does (D-S1).
            reinvest(batch, book=Book.PAPER)
            return

    def _divergence(self, day: date, instruments: dict) -> None:
        """An unresolved divergence, so M31b withholding is visible."""
        from gains.fifo import available
        from journal.models import Book
        from records.models import Divergence

        symbol = next(
            (s for s, *_ in UNIVERSE if available(s, book=Book.PAPER) >= 2),
            None,
        )
        if symbol is None:
            return

        Divergence.objects.create(
            instrument=instruments[symbol],
            expected_quantity=available(symbol, book=Book.PAPER),
            actual_quantity=max(available(symbol, book=Book.PAPER) - 1, 0),
            detected_on=day,
            trace_id=trace_id_for(day),
        )

    def _broker_holdings(self, instruments: dict) -> None:
        """What the broker says it holds.

        Made to agree with the lots everywhere except the one instrument
        carrying a divergence -- so the share ledger shows a clean reconciliation
        with exactly one disagreement, which is the state worth looking at.
        """
        from gains.fifo import available
        from journal.models import Book
        from portfolio.models import Holding
        from records.divergence import is_in_doubt

        for symbol in instruments:
            held = available(symbol, book=Book.PAPER)
            if held == 0:
                continue
            Holding.objects.update_or_create(
                instrument_id=symbol,
                defaults={
                    "quantity": held - 1 if is_in_doubt(symbol) else held,
                    "average_cost": Decimal("0"),
                },
            )
