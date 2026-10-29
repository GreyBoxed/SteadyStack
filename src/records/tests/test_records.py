"""External movements, divergences, and finalising a basis.

The refusals are the interesting part. A back-dated buy that would change
which lots an already-matched sale consumed is refused rather than absorbed;
a divergence is classified by a person rather than guessed at; and a figure
for an instrument in doubt is withheld rather than computed from a holding
known to be wrong.
"""

from datetime import date
from decimal import Decimal

import pytest
from django.db import DatabaseError

from audit.models import AuditEvent
from gains import fifo
from gains.models import RealisedGain
from portfolio import contract_note
from portfolio.instruments import refresh
from portfolio.models import BasisState, Holding, Instrument, Lot
from records import divergence as div
from records import external
from records.external import ExternalRefused
from records.models import Correction, DivergenceClassification, ExternalSide, ExternalTransaction

pytestmark = pytest.mark.django_db

JAN = date(2024, 1, 15)
JUN = date(2024, 6, 20)
SEP = date(2026, 9, 22)


@pytest.fixture
def instrument(db):
    refresh(
        [
            {
                "tradingsymbol": "INFY",
                "instrument_token": 408065,
                "name": "INFY",
                "exchange": "NSE",
                "instrument_type": "EQ",
                "segment": "NSE",
                "tick_size": 0.05,
                "lot_size": 1,
            }
        ]
    )
    return Instrument.objects.get(pk="INFY")


def a_lot(instrument, *, acquired_on=JAN, quantity=5, cost="1000", state=BasisState.FINAL):
    return Lot.objects.create(
        instrument=instrument,
        acquired_on=acquired_on,
        quantity=quantity,
        remaining_quantity=quantity,
        cost_per_unit=Decimal(cost),
        basis_state=state,
    )


# -- external buys and transfers in ---------------------------------------


def test_an_external_buy_creates_a_lot(instrument):
    result = external.record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=10,
        price=Decimal("1200"),
        charges=Decimal("20"),
        occurred_on=SEP,
        acquired_on=SEP,
        actor="operator",
        reason="bought on the broker's app",
    )

    assert result.lot is not None
    assert result.lot.quantity == 10
    assert result.lot.cost_per_unit == Decimal("1202.0000")


def test_a_transfer_in_keeps_the_transferors_clock(instrument):
    """The shares did not start existing when they arrived."""
    result = external.record_external(
        instrument=instrument,
        side=ExternalSide.TRANSFER_IN,
        quantity=5,
        price=Decimal("900"),
        occurred_on=SEP,
        acquired_on=JAN,
        actor="operator",
        reason="transferred from another demat",
    )

    assert result.lot.acquired_on == JAN


def test_an_externally_created_lot_is_not_waiting_for_a_contract_note(instrument):
    result = external.record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=1,
        price=Decimal("1200"),
        occurred_on=SEP,
        actor="operator",
        reason="bought elsewhere",
    )

    assert result.lot.basis_state == BasisState.IMPORTED


def test_recording_is_audited(instrument):
    external.record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=1,
        price=Decimal("1200"),
        occurred_on=SEP,
        actor="operator",
        reason="bought elsewhere",
    )

    assert AuditEvent.objects.filter(action="external.recorded").exists()


# -- external sales and transfers out -------------------------------------


def test_an_external_sale_consumes_lots_fifo(instrument):
    a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")
    a_lot(instrument, acquired_on=JUN, quantity=5, cost="1500")

    result = external.record_external(
        instrument=instrument,
        side=ExternalSide.SELL,
        quantity=7,
        price=Decimal("2000"),
        occurred_on=SEP,
        actor="operator",
        reason="sold on the broker's app",
    )

    assert result.consumed.quantity == 7
    assert len(result.consumed.realisations) == 2


def test_a_transfer_out_is_a_disposal_at_nil_proceeds(instrument):
    a_lot(instrument, quantity=5, cost="1000")

    result = external.record_external(
        instrument=instrument,
        side=ExternalSide.TRANSFER_OUT,
        quantity=5,
        occurred_on=SEP,
        actor="operator",
        reason="gifted",
    )

    assert result.consumed.quantity == 5
    assert RealisedGain.objects.first().net_proceeds == Decimal("0.0000")


def test_a_transfer_out_is_flagged_for_a_person_to_decide(instrument):
    """What a transfer out was *for* is not this system's decision."""
    a_lot(instrument, quantity=5, cost="1000")

    result = external.record_external(
        instrument=instrument,
        side=ExternalSide.TRANSFER_OUT,
        quantity=5,
        occurred_on=SEP,
        actor="operator",
        reason="gifted",
    )

    assert result.transaction.needs_manual_treatment is True


def test_selling_more_than_the_lots_hold_is_refused(instrument):
    a_lot(instrument, quantity=2, cost="1000")

    with pytest.raises(fifo.InsufficientLots):
        external.record_external(
            instrument=instrument,
            side=ExternalSide.SELL,
            quantity=5,
            price=Decimal("2000"),
            occurred_on=SEP,
            actor="operator",
            reason="sold elsewhere",
        )


# -- the back-dated buy (M36a) --------------------------------------------


def test_a_back_dated_buy_before_a_matched_sale_is_refused(instrument):
    """Accepting it would change which lots that sale consumed, and those
    realised rows are immutable by construction."""
    a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")
    external.record_external(
        instrument=instrument,
        side=ExternalSide.SELL,
        quantity=2,
        price=Decimal("2000"),
        occurred_on=JUN,
        actor="operator",
        reason="sold elsewhere",
    )

    with pytest.raises(ExternalRefused, match="correction instead"):
        external.record_external(
            instrument=instrument,
            side=ExternalSide.BUY,
            quantity=1,
            price=Decimal("900"),
            occurred_on=SEP,
            acquired_on=date(2024, 2, 1),
            actor="operator",
            reason="found an old contract note",
        )


def test_a_buy_after_the_last_matched_sale_is_accepted(instrument):
    a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")
    external.record_external(
        instrument=instrument,
        side=ExternalSide.SELL,
        quantity=2,
        price=Decimal("2000"),
        occurred_on=JUN,
        actor="operator",
        reason="sold elsewhere",
    )

    result = external.record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=1,
        price=Decimal("900"),
        occurred_on=SEP,
        acquired_on=SEP,
        actor="operator",
        reason="bought elsewhere",
    )

    assert result.lot is not None


def test_a_refused_buy_records_nothing(instrument):
    a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")
    external.record_external(
        instrument=instrument,
        side=ExternalSide.SELL,
        quantity=2,
        price=Decimal("2000"),
        occurred_on=JUN,
        actor="operator",
        reason="sold elsewhere",
    )
    before = ExternalTransaction.objects.count()

    with pytest.raises(ExternalRefused):
        external.record_external(
            instrument=instrument,
            side=ExternalSide.BUY,
            quantity=1,
            price=Decimal("900"),
            occurred_on=SEP,
            acquired_on=date(2024, 2, 1),
            actor="operator",
            reason="too late",
        )

    assert ExternalTransaction.objects.count() == before


# -- bonus issues (S8) -----------------------------------------------------


def test_a_bonus_creates_lots_at_nil_cost(instrument):
    a_lot(instrument, acquired_on=JAN, quantity=10, cost="1000")

    created = external.bonus_issue(
        instrument=instrument, quantity=10, occurred_on=SEP, actor="operator"
    )

    assert len(created) == 1
    assert created[0].cost_per_unit == Decimal("0.0000")


def test_bonus_shares_inherit_the_dates_that_earned_them(instrument):
    """Dating them from the issue would misstate how long the position has
    been held."""
    a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")
    a_lot(instrument, acquired_on=JUN, quantity=5, cost="1500")

    created = external.bonus_issue(
        instrument=instrument, quantity=10, occurred_on=SEP, actor="operator"
    )

    assert sorted(lot.acquired_on for lot in created) == [JAN, JUN]


def test_a_bonus_allocates_the_whole_quantity(instrument):
    a_lot(instrument, acquired_on=JAN, quantity=3, cost="1000")
    a_lot(instrument, acquired_on=JUN, quantity=4, cost="1500")

    created = external.bonus_issue(
        instrument=instrument, quantity=7, occurred_on=SEP, actor="operator"
    )

    assert sum(lot.quantity for lot in created) == 7


def test_a_bonus_on_nothing_is_refused(instrument):
    with pytest.raises(ExternalRefused, match="holding of nothing"):
        external.bonus_issue(instrument=instrument, quantity=10, occurred_on=SEP, actor="operator")


# -- divergence ------------------------------------------------------------


def test_agreement_raises_nothing(instrument):
    a_lot(instrument, quantity=5, cost="1000")
    Holding.objects.create(instrument=instrument, quantity=5)

    result = div.scan(detected_on=SEP)

    assert result.opened == []


def test_a_difference_is_raised(instrument):
    a_lot(instrument, quantity=5, cost="1000")
    Holding.objects.create(instrument=instrument, quantity=8)

    result = div.scan(detected_on=SEP)

    assert len(result.opened) == 1
    assert result.opened[0].difference == 3


def test_a_difference_is_never_guessed_at(instrument):
    """The same three shares could be a purchase, a transfer, a corporate
    action or a bug in this program."""
    a_lot(instrument, quantity=5, cost="1000")
    Holding.objects.create(instrument=instrument, quantity=8)

    div.scan(detected_on=SEP)

    assert div.open_divergences("INFY").first().classification == (
        DivergenceClassification.UNCLASSIFIED
    )


def test_a_divergence_is_not_raised_twice(instrument):
    a_lot(instrument, quantity=5, cost="1000")
    Holding.objects.create(instrument=instrument, quantity=8)

    div.scan(detected_on=SEP)
    div.scan(detected_on=SEP)

    assert div.open_divergences("INFY").count() == 1


def test_shares_bought_today_are_not_yet_a_divergence(instrument):
    """M31c. A CNC buy credits after settlement, so the broker showing fewer
    shares than the lots hold on the evening of the trade is correct.

    Reporting it would raise the same alarm every day the ladder filled, and
    a recurring false alarm is worse than no alarm -- it teaches the operator
    to wave the real one through.
    """
    a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")
    a_lot(instrument, acquired_on=SEP, quantity=3, cost="1400")
    Holding.objects.create(instrument=instrument, quantity=5)

    result = div.scan(detected_on=SEP)

    assert result.opened == []


def test_shares_bought_yesterday_are_due_and_are_compared(instrument):
    """The deferral lasts exactly as long as settlement does."""
    a_lot(instrument, acquired_on=date(2026, 9, 21), quantity=3, cost="1400")
    Holding.objects.create(instrument=instrument, quantity=0)

    result = div.scan(detected_on=SEP)

    assert len(result.opened) == 1
    assert result.opened[0].expected_quantity == 3


def test_a_settlement_holiday_delays_the_comparison_by_a_day(instrument):
    """The case a trading calendar alone cannot answer: the market opened,
    the clearing house did not."""
    from trading_calendar.models import SessionKind
    from trading_calendar.sessions import set_day

    set_day(SEP, kind=SessionKind.NORMAL, settles=False, note="settlement holiday")
    a_lot(instrument, acquired_on=date(2026, 9, 21), quantity=3, cost="1400")
    Holding.objects.create(instrument=instrument, quantity=0)

    result = div.scan(detected_on=SEP)

    assert result.opened == [], "the shares are not due until the next settling day"


def test_a_divergence_that_resolves_itself_is_closed(instrument):
    """Settlement lagging a postback looks exactly like a real one for an
    evening."""
    a_lot(instrument, quantity=5, cost="1000")
    holding = Holding.objects.create(instrument=instrument, quantity=8)
    div.scan(detected_on=SEP)

    holding.quantity = 5
    holding.save()
    result = div.scan(detected_on=SEP)

    assert len(result.closed) == 1
    assert div.is_in_doubt("INFY") is False


def test_an_instruments_figures_are_withheld_while_it_is_in_doubt(instrument):
    a_lot(instrument, quantity=5, cost="1000")
    Holding.objects.create(instrument=instrument, quantity=8)
    div.scan(detected_on=SEP)

    with pytest.raises(div.DivergenceUnresolved, match="withheld"):
        div.require_settled("INFY", what="Unrealised gain")


def test_portfolio_figures_are_still_produced_but_flagged(instrument):
    """Withholding everything because one name is in doubt would be its own
    kind of lie (M31b)."""
    a_lot(instrument, quantity=5, cost="1000")
    Holding.objects.create(instrument=instrument, quantity=8)
    div.scan(detected_on=SEP)

    caveats = div.portfolio_caveats()

    assert caveats[0]["instrument"] == "INFY"
    assert caveats[0]["difference"] == 3


def test_classifying_resolves_it(instrument):
    a_lot(instrument, quantity=5, cost="1000")
    Holding.objects.create(instrument=instrument, quantity=8)
    divergence = div.scan(detected_on=SEP).opened[0]

    div.classify(
        divergence,
        classification=DivergenceClassification.EXTERNAL_BUY,
        actor="operator",
        note="bought on the phone",
    )

    assert div.is_in_doubt("INFY") is False
    assert AuditEvent.objects.filter(action="divergence.classified").exists()


def test_a_classification_can_name_the_entry_that_explains_it(instrument):
    a_lot(instrument, quantity=5, cost="1000")
    Holding.objects.create(instrument=instrument, quantity=8)
    divergence = div.scan(detected_on=SEP).opened[0]

    entry = external.record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=3,
        price=Decimal("1200"),
        occurred_on=SEP,
        actor="operator",
        reason="bought on the phone",
    ).transaction

    div.classify(
        divergence,
        classification=DivergenceClassification.EXTERNAL_BUY,
        actor="operator",
        explained_by=entry,
    )

    divergence.refresh_from_db()
    assert divergence.explained_by_id == entry.id


def test_classifying_as_unclassified_explains_nothing(instrument):
    a_lot(instrument, quantity=5, cost="1000")
    Holding.objects.create(instrument=instrument, quantity=8)
    divergence = div.scan(detected_on=SEP).opened[0]

    with pytest.raises(ValueError, match="explains nothing"):
        div.classify(
            divergence,
            classification=DivergenceClassification.UNCLASSIFIED,
            actor="operator",
        )


# -- finalising a basis (M35b) --------------------------------------------


def test_finalising_supersedes_rather_than_edits(instrument):
    lot = a_lot(instrument, quantity=2, cost="1005", state=BasisState.ESTIMATED)

    result = contract_note.finalise(lot, actual_charges=Decimal("12"), actor="operator")

    lot.refresh_from_db()
    assert lot.superseded_by_id == result.replacement.id
    assert result.replacement.basis_state == BasisState.FINAL


def test_the_replacement_keeps_the_original_clock(instrument):
    lot = a_lot(instrument, acquired_on=JAN, quantity=2, cost="1005", state=BasisState.ESTIMATED)

    result = contract_note.finalise(lot, actual_charges=Decimal("12"), actor="operator")

    assert result.replacement.acquired_on == JAN
    assert result.replacement.quantity == 2


def test_finalising_records_a_correction(instrument):
    lot = a_lot(instrument, quantity=2, cost="1005", state=BasisState.ESTIMATED)

    contract_note.finalise(
        lot, actual_charges=Decimal("12"), actor="operator", note_reference="CN-991"
    )

    correction = Correction.objects.get()
    assert correction.target_table == "portfolio.Lot"
    assert "CN-991" in correction.reason


def test_a_partly_consumed_lot_keeps_what_is_left(instrument):
    lot = a_lot(instrument, quantity=5, cost="1005", state=BasisState.ESTIMATED)
    Lot.objects.filter(pk=lot.pk).update(remaining_quantity=2)
    lot.refresh_from_db()

    result = contract_note.finalise(lot, actual_charges=Decimal("12"), actor="operator")

    assert result.replacement.remaining_quantity == 2
    assert result.replacement.quantity == 5


def test_finalising_twice_is_refused(instrument):
    lot = a_lot(instrument, quantity=2, cost="1005", state=BasisState.ESTIMATED)
    contract_note.finalise(lot, actual_charges=Decimal("12"), actor="operator")
    lot.refresh_from_db()

    with pytest.raises(contract_note.FinalisationRefused):
        contract_note.finalise(lot, actual_charges=Decimal("15"), actor="operator")


def test_a_final_basis_is_not_finalised_again(instrument):
    lot = a_lot(instrument, quantity=2, cost="1000", state=BasisState.FINAL)

    with pytest.raises(contract_note.FinalisationRefused, match="already final"):
        contract_note.finalise(lot, actual_charges=Decimal("12"), actor="operator")


def test_finalising_carries_the_external_transaction_link_to_the_replacement(instrument):
    """`external_transaction` is a plain `ForeignKey`, not a `OneToOneField`:
    the original and the replacement both describe the same purchase, so both
    keep the link -- the original does not lose its provenance."""
    result = external.record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=2,
        price=Decimal("1200"),
        occurred_on=SEP,
        actor="operator",
        reason="bought elsewhere",
    )
    lot = result.lot
    Lot.objects.filter(pk=lot.pk).update(basis_state=BasisState.ESTIMATED)
    lot.refresh_from_db()

    finalisation = contract_note.finalise(lot, actual_charges=Decimal("12"), actor="operator")

    lot.refresh_from_db()
    assert lot.external_transaction_id == result.transaction.id
    assert finalisation.replacement.external_transaction_id == result.transaction.id


def test_a_lots_external_transaction_link_cannot_be_rewritten(instrument):
    """It is part of the lot's identity, alongside the fill it came from
    (0006): repointing it would make the lot claim a different movement."""
    first = external.record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=1,
        price=Decimal("100"),
        occurred_on=SEP,
        actor="operator",
        reason="bought elsewhere",
    ).transaction
    second = external.record_external(
        instrument=instrument,
        side=ExternalSide.BUY,
        quantity=1,
        price=Decimal("200"),
        occurred_on=SEP,
        actor="operator",
        reason="bought elsewhere too",
    ).transaction
    lot = Lot.objects.get(external_transaction=first)

    with pytest.raises(DatabaseError, match="immutable basis"):
        Lot.objects.filter(pk=lot.pk).update(external_transaction=second)


def test_lots_awaiting_a_note_are_listable(instrument):
    a_lot(instrument, quantity=2, cost="1005", state=BasisState.ESTIMATED)
    a_lot(instrument, quantity=2, cost="1000", state=BasisState.FINAL)

    assert contract_note.estimated_lots().count() == 1
    assert contract_note.has_estimated_basis("INFY") is True


# -- splits and consolidations (M36b) -------------------------------------


def test_a_split_supersedes_rather_than_edits(instrument):
    from records import corporate_actions

    original = a_lot(instrument, acquired_on=JAN, quantity=10, cost="1000")

    result = corporate_actions.split(
        instrument=instrument, factor=5, effective_on=SEP, actor="operator"
    )

    original.refresh_from_db()
    assert original.superseded_by_id == result.created[0].id
    assert original.cost_per_unit == Decimal("1000.0000")


def test_a_split_multiplies_the_shares_and_divides_the_cost(instrument):
    """The same money, divided differently: nothing was bought or sold."""
    from records import corporate_actions

    a_lot(instrument, acquired_on=JAN, quantity=10, cost="1000")

    result = corporate_actions.split(
        instrument=instrument, factor=5, effective_on=SEP, actor="operator"
    )

    assert result.quantity_after == 50
    assert result.created[0].cost_per_unit == Decimal("200.0000")


def test_a_split_preserves_the_total_cost_to_within_rounding(instrument):
    """7 shares into 21 is 333.3333... recurring: no per-share cost stored to
    four places can carry it exactly. The error is bounded by the last stored
    place, which is what a per-share basis costs -- claiming exactness here
    would be a comment that is wrong."""
    from records import corporate_actions

    a_lot(instrument, acquired_on=JAN, quantity=7, cost="1000")

    result = corporate_actions.split(
        instrument=instrument, factor=3, effective_on=SEP, actor="operator"
    )

    drift = abs(result.cost_after - result.cost_before)
    assert drift < Decimal("0.01")


def test_a_split_that_divides_evenly_loses_nothing(instrument):
    from records import corporate_actions

    a_lot(instrument, acquired_on=JAN, quantity=10, cost="1000")

    result = corporate_actions.split(
        instrument=instrument, factor=5, effective_on=SEP, actor="operator"
    )

    assert result.cost_after == result.cost_before


def test_split_lots_keep_the_original_acquisition_date(instrument):
    """Dating them from the split would say the position was acquired on the
    day the company restructured its share capital."""
    from records import corporate_actions

    a_lot(instrument, acquired_on=JAN, quantity=10, cost="1000")

    result = corporate_actions.split(
        instrument=instrument, factor=5, effective_on=SEP, actor="operator"
    )

    assert result.created[0].acquired_on == JAN


def test_a_consolidation_reduces_the_shares(instrument):
    from records import corporate_actions

    a_lot(instrument, acquired_on=JAN, quantity=100, cost="10")

    result = corporate_actions.consolidate(
        instrument=instrument, factor=10, effective_on=SEP, actor="operator"
    )

    assert result.quantity_after == 10
    assert result.cost_after == result.cost_before


def test_a_consolidation_that_would_wipe_a_lot_out_is_refused(instrument):
    """A fractional-share outcome is a decision for a person."""
    from records import corporate_actions

    a_lot(instrument, acquired_on=JAN, quantity=5, cost="1000")

    with pytest.raises(corporate_actions.CorporateActionRefused, match="fractional"):
        corporate_actions.consolidate(
            instrument=instrument, factor=10, effective_on=SEP, actor="operator"
        )


def test_a_split_records_a_correction_per_lot(instrument):
    from records import corporate_actions

    a_lot(instrument, acquired_on=JAN, quantity=10, cost="1000")
    a_lot(instrument, acquired_on=JUN, quantity=10, cost="1500")

    corporate_actions.split(instrument=instrument, factor=2, effective_on=SEP, actor="operator")

    assert Correction.objects.filter(target_table="portfolio.Lot").count() == 2


def test_a_split_with_no_lots_is_refused(instrument):
    from records import corporate_actions

    with pytest.raises(corporate_actions.CorporateActionRefused, match="no open lots"):
        corporate_actions.split(instrument=instrument, factor=5, effective_on=SEP, actor="operator")


def test_split_lots_are_the_ones_fifo_consumes(instrument):
    """The originals are superseded and must not be sold twice."""
    from records import corporate_actions

    a_lot(instrument, acquired_on=JAN, quantity=10, cost="1000")
    corporate_actions.split(instrument=instrument, factor=5, effective_on=SEP, actor="operator")

    assert fifo.available("INFY") == 50


def a_filled_lot(instrument, *, quantity=2, price=Decimal("1000")):
    """A lot as the system really makes one: from a fill, basis estimated.

    Finalisation can only recover the gross from a fill, which is the only way
    an estimated basis ever arises.
    """
    from django.utils import timezone

    from engine.models import TradeSheet, TradeSheetState
    from journal.trades import post_fill
    from orders.models import Fill, Order, OrderMode, OrderType
    from portfolio import lots as lot_writer

    sheet = TradeSheet.objects.create(
        trading_date=SEP,
        version=1,
        trace_id="ts-2026-09-22",
        state=TradeSheetState.RUNNING,
        instrument=instrument,
        quantity=quantity,
    )
    order = Order.objects.create(
        tag=f"tx-note-{quantity}",
        tradesheet=sheet,
        instrument=instrument,
        trading_date=SEP,
        tick_no=0,
        mode=OrderMode.PAPER,
        order_type=OrderType.LIMIT,
        quantity=quantity,
        limit_price=price,
    )
    fill = Fill.objects.create(
        order=order,
        kite_trade_id=f"paper-note-{quantity}",
        quantity=quantity,
        price=price,
        executed_at=timezone.now(),
    )
    lot = lot_writer.from_fill(fill)
    post_fill(fill)
    return lot


def correction_lines():
    from journal.models import EntryKind, JournalLine

    return {
        line.account_id: line.amount
        for line in JournalLine.objects.filter(entry__kind=EntryKind.CHARGE_CORRECTION)
    }


def test_finalising_posts_the_difference_as_a_charge_correction(instrument):
    """The money has already moved and the fill's entry is never rewritten, so
    the difference is a new entry, dated when it is known (M35b)."""
    from journal.models import EntryKind, JournalEntry

    lot = a_filled_lot(instrument, quantity=2, price=Decimal("1000"))
    estimated = (lot.cost_per_unit * lot.quantity) - Decimal("2000")

    contract_note.finalise(lot, actual_charges=estimated + Decimal("18"), actor="operator")

    entry = JournalEntry.objects.get(kind=EntryKind.CHARGE_CORRECTION)
    assert entry.source_type == "lot"
    assert entry.source_id == str(lot.id)
    # An engine buy: the cash leaves the engine account, the cost joins the shares.
    assert correction_lines() == {"1110": Decimal("-18"), "1300": Decimal("18")}


def test_a_note_charging_less_gives_the_cash_back(instrument):
    lot = a_filled_lot(instrument, quantity=2, price=Decimal("1000"))
    estimated = (lot.cost_per_unit * lot.quantity) - Decimal("2000")

    contract_note.finalise(lot, actual_charges=estimated - Decimal("6"), actor="operator")

    assert correction_lines()["1110"] == Decimal("6")


def test_a_note_that_matches_the_estimate_posts_nothing(instrument):
    """Nothing moved, so nothing is owed."""
    from journal.models import EntryKind, JournalEntry

    lot = a_filled_lot(instrument, quantity=2, price=Decimal("1000"))
    estimated = (lot.cost_per_unit * lot.quantity) - Decimal("2000")

    contract_note.finalise(lot, actual_charges=estimated, actor="operator")

    assert not JournalEntry.objects.filter(kind=EntryKind.CHARGE_CORRECTION).exists()


def posted_charges(lot):
    """The charges the fill's journal entry already carries for this lot."""
    from journal.models import JournalLine

    lines = JournalLine.objects.filter(
        entry__idempotency_key=f"fill:{lot.created_from_fill.id}", account_id="1300"
    ).exclude(component="gross")
    return sum((line.amount for line in lines), Decimal("0"))


def test_a_note_equal_to_the_posted_charges_posts_nothing(instrument):
    """The lot's basis is rounded to four places; the journal's charges are
    not. Measured against the basis, three shares would leave sub-paisa noise
    that is not a real difference."""
    from journal.models import EntryKind, JournalEntry

    lot = a_filled_lot(instrument, quantity=3, price=Decimal("1234.55"))
    charges = posted_charges(lot)
    assert (lot.cost_per_unit * 3) - Decimal("3703.65") != charges  # the noise is real

    contract_note.finalise(lot, actual_charges=charges, actor="operator")

    assert not JournalEntry.objects.filter(kind=EntryKind.CHARGE_CORRECTION).exists()


def test_a_correction_on_a_partly_sold_lot_splits_between_cost_and_gain(instrument):
    """Three of four shares are still held: three quarters of the difference
    is their cost, the quarter already sold adjusts its realised gain."""
    lot = a_filled_lot(instrument, quantity=4, price=Decimal("1000"))
    # One share sold, as test_a_partly_consumed_lot_keeps_what_is_left has it.
    Lot.objects.filter(pk=lot.pk).update(remaining_quantity=3)
    lot.refresh_from_db()
    assert lot.remaining_quantity == 3

    contract_note.finalise(lot, actual_charges=posted_charges(lot) + Decimal("8"), actor="operator")

    assert correction_lines() == {
        "1300": Decimal("6"),
        "4100": Decimal("2"),
        "1110": Decimal("-8"),
    }


def test_the_replacement_basis_reflects_the_real_charges(instrument):
    lot = a_filled_lot(instrument, quantity=2, price=Decimal("1000"))

    result = contract_note.finalise(lot, actual_charges=Decimal("40"), actor="operator")

    assert result.replacement.cost_per_unit == Decimal("1020.0000")


# -- a correction is itself append-only (M36c, §7) -------------------------


def a_correction():
    return Correction.objects.create(
        target_table="portfolio_lot",
        target_id="0f5d1e4a",
        reason="the quantity was entered twice",
        actor="operator",
        before={"quantity": 4},
        after={"quantity": 2},
    )


@pytest.mark.django_db(transaction=True)
def test_a_correction_cannot_be_rewritten():
    """The reason a correction states is the whole evidence that an error was
    fixed rather than covered up. A correction that can be edited proves
    nothing."""
    a_correction()

    with pytest.raises(DatabaseError, match="append-only"):
        Correction.objects.update(reason="something else entirely")


@pytest.mark.django_db(transaction=True)
def test_a_correction_cannot_be_deleted():
    a_correction()

    with pytest.raises(DatabaseError, match="append-only"):
        Correction.objects.all().delete()


# -- holdings that are not the portfolio's (go-live r2, Nm4) ---------------


def _model_of(*instruments):
    from model_portfolio.models import ModelPortfolio, ModelPortfolioRow

    portfolio = ModelPortfolio.objects.create(version=1, uploaded_by="op", content_hash="x")
    for each in instruments:
        ModelPortfolioRow.objects.create(
            portfolio=portfolio, instrument=each, target_pct=Decimal("100")
        )
    return portfolio


def test_a_personal_holding_outside_the_model_is_listed_not_raised(instrument):
    """The adult's own shares in the same demat: no lots, not a model name."""
    tcs = Instrument.objects.create(tradingsymbol="TCS", instrument_token=2953217, name="TCS")
    _model_of(instrument)
    Holding.objects.create(instrument=tcs, quantity=12)

    result = div.scan(detected_on=SEP)

    assert result.opened == []
    assert [holding.instrument_id for holding in result.not_ours] == ["TCS"]
    assert [holding.instrument_id for holding in div.not_the_portfolios()] == ["TCS"]


def test_a_model_name_held_without_lots_is_still_a_divergence(instrument):
    _model_of(instrument)
    Holding.objects.create(instrument=instrument, quantity=4)

    result = div.scan(detected_on=SEP)

    assert [row.instrument_id for row in result.opened] == ["INFY"]
    assert div.not_the_portfolios() == []


def test_a_name_with_lots_outside_the_model_is_still_compared(instrument):
    """Dropped from the model, but the portfolio held it: its lots still count."""
    tcs = Instrument.objects.create(tradingsymbol="TCS", instrument_token=2953217, name="TCS")
    _model_of(tcs)
    a_lot(instrument, quantity=5, cost="1000")
    Holding.objects.create(instrument=instrument, quantity=8)

    assert [row.instrument_id for row in div.scan(detected_on=SEP).opened] == ["INFY"]
