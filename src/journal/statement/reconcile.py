"""Matching statement lines to entries, and posting the ones nothing explains."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import connection, transaction

from audit.context import today_ist
from journal import cash
from journal.accounts import ENGINE_CASH, REALISED, UNALLOCATED
from journal.models import (
    Book,
    Component,
    EntryKind,
    JournalEntry,
    JournalLine,
    StatementLine,
    StatementUpload,
)
from journal.periods import is_closed
from journal.posting import Line, post, q6
from journal.statement.classify import propose
from journal.statement.store import link, open_to_matching

ZERO = Decimal("0")
DEBIT_KINDS = {"withdrawal", "sweep", "owner_expense", "account_charge"}


class ReconcileError(Exception):
    pass


def queue():
    """Lines no standing entry explains, including any whose entry was reversed."""
    return StatementLine.objects.filter(open_to_matching()).order_by(
        "posting_date", "upload__uploaded_at", "position"
    )


def _unclaimed(entries):
    return entries.exclude(statement_lines__isnull=False)


def _standing(line: StatementLine) -> JournalEntry | None:
    """The entry *line* is matched to, unless that entry has been reversed."""
    entry = line.matched_entry
    if entry is None or JournalEntry.objects.filter(reverses=entry).exists():
        return None
    return entry


def _cash_effect(entry: JournalEntry) -> Decimal:
    lines = entry.lines.filter(account_id__in=[UNALLOCATED, ENGINE_CASH])
    return sum((line.amount for line in lines), ZERO)


def match_for(line: StatementLine) -> JournalEntry | None:
    """An existing entry that is this line, if there is exactly one."""
    from trading_calendar.sessions import previous_trading_day

    proposal = propose(line)
    # A reversed entry, or a reversal, explains no line.
    live = JournalEntry.objects.filter(book=Book.LIVE, reversed_by__isnull=True).exclude(
        kind=EntryKind.REVERSAL
    )

    if proposal.kind == "contribution":
        candidates = _unclaimed(live.filter(kind=EntryKind.CONTRIBUTION))
        if proposal.reference:
            by_reference = candidates.filter(source_id=proposal.reference)
            if by_reference.count() == 1:
                # Spec §6: matched by reference number *and* amount -- a
                # reference match that disagrees on amount is not this line's
                # entry, and is not a case to fall back to nearby-by-amount
                # for either: something is wrong, so it waits in the queue.
                entry = by_reference.first()
                return entry if _cash_effect(entry) == line.credit else None
        nearby = [
            entry
            for entry in candidates.filter(
                occurred_on__range=(
                    line.posting_date - timedelta(days=3),
                    line.posting_date + timedelta(days=3),
                )
            )
            if _cash_effect(entry) == line.credit
        ]
        return nearby[0] if len(nearby) == 1 else None

    if proposal.kind == "settlement":
        # Only an entry no standing line holds (NI1): a day whose own entry
        # was never posted must not borrow the day before's, whose line
        # already claims it -- that reads as a whole-day disagreement.
        settlements = _unclaimed(live.filter(kind=EntryKind.SETTLEMENT))
        for day in (line.posting_date, previous_trading_day(line.posting_date)):
            if day is None:
                continue
            entry = settlements.filter(occurred_on=day).order_by("-number").first()
            if entry is not None:
                return entry

    if proposal.kind == "dp_charge":
        # Zerodha debits the DP charge on its own line; ours sits inside the
        # first sell of the day for that scrip, as its `dp` component.
        for day in (line.posting_date, previous_trading_day(line.posting_date)):
            if day is None:
                continue
            sells = _unclaimed(live.filter(kind=EntryKind.SELL_FILL, occurred_on=day))
            for entry in sells.order_by("number"):
                dp = entry.lines.filter(component=Component.DP)
                if dp.exists() and q6(sum((ln.amount for ln in dp), ZERO)) == line.debit:
                    return entry
    return None


def _before_opening(line: StatementLine, opening: JournalEntry | None) -> bool:
    """Whether the opening's cash already holds *line*'s movement (I6).

    `open_books --cash` is the funds balance at the close of its date, so a
    line posted on or before that date is inside it.
    """
    return opening is not None and line.posting_date <= opening.occurred_on


@transaction.atomic
def auto_match(upload) -> int:
    """Link every line of *upload* that an existing entry already explains.

    A settlement is linked whether or not the amounts agree: the difference is
    the charges check (Task 13), not a reason to leave the line unexplained.
    A line dated on or before the opening that no entry explains is linked to
    the opening itself: its money is inside the opening's cash, and posting it
    again would count it twice. Many lines may point at the one opening.
    """
    from journal.external import standing_opening

    opening = standing_opening()
    linked = 0
    for line in upload.lines.filter(open_to_matching()):
        entry = match_for(line)
        if entry is None and _before_opening(line, opening):
            entry = opening
        if entry is not None:
            link(line, entry)
            linked += 1
    return linked


def link_to_opening(opening: JournalEntry) -> int:
    """Link every waiting line dated on or before *opening* to it.

    For lines uploaded before the books were opened, which would otherwise
    wait for ever: confirming them is refused, since the opening holds them.
    """
    linked = 0
    waiting = StatementLine.objects.filter(open_to_matching()).filter(
        posting_date__lte=opening.occurred_on
    )
    for line in waiting:
        if match_for(line) is None:
            link(line, opening)
            linked += 1
    return linked


def opening_disagreement() -> dict | None:
    """Zerodha's balance at the opening date by the statements, against ours.

    The statements' balance is the running balance of the last line on or
    before the opening date -- or, when every line held is later, the first
    upload's opening balance. None when there is no opening, no statement, or
    the two agree to the rupee (the morning check's tolerance).
    """
    from journal.balances import zerodha_cash
    from journal.checks import TOLERANCE
    from journal.external import standing_opening

    opening = standing_opening()
    if opening is None:
        return None
    on = opening.occurred_on
    last = (
        StatementLine.objects.filter(posting_date__lte=on)
        .order_by("-posting_date", "-upload__uploaded_at", "-position")
        .first()
    )
    if last is not None:
        theirs = last.net_balance
    else:
        first = StatementLine.objects.order_by("posting_date", "upload__uploaded_at", "position")
        first = first.select_related("upload").first()
        if first is None:
            return None
        theirs = first.upload.opening_balance
    ours = zerodha_cash(Book.LIVE, through=on)
    if abs(theirs - ours) <= TOLERANCE:
        return None
    return {"on": on, "theirs": q6(theirs), "ours": ours, "difference": q6(theirs - ours)}


@transaction.atomic
def confirm(line: StatementLine, *, kind: str, actor: str, component: str = "") -> JournalEntry:
    """Post the entry a person says this line is, and link it."""
    line.refresh_from_db()
    standing = _standing(line)
    if standing is not None:
        return standing

    from journal.external import standing_opening

    opening = standing_opening()
    if _before_opening(line, opening):
        raise ReconcileError(
            f"This line is dated on or before the opening balance of "
            f"{opening.occurred_on}, which already holds it: it is not posted again."
        )

    key = f"statement:{line.fingerprint}"
    if line.matched_entry_id is not None:
        # Confirmed before as the wrong kind and reversed since: a new key, or
        # the reversed entry would come straight back.
        key = f"{key}:after:{line.matched_entry_id}"

    # A line in a year the CA has closed posts today, as a prior-period item:
    # a late fact is dated when it is known (spec §6).
    on, late = line.posting_date, ""
    if is_closed(Book.LIVE, on):
        on = today_ist()
        late = f" (relates to {line.posting_date.isoformat()}, after the year close)"
    common = {"on": on, "actor": actor, "idempotency_key": key}

    if kind == "contribution":
        if line.credit <= ZERO:
            raise ReconcileError("Only money arriving can be a contribution.")
        reference = propose(line).reference
        entry = cash.contribute(
            line.credit,
            reference=reference,
            description=f"contribution {reference}".strip() + late,
            source_type="statement",
            source_id=line.fingerprint,
            **common,
        )
    elif kind in DEBIT_KINDS:
        if line.debit <= ZERO:
            raise ReconcileError(f"A {kind.replace('_', ' ')} is money leaving.")
        if kind == "account_charge":
            entry = cash.account_charge(
                line.debit,
                component=component or propose(line).component or "other",
                description=line.particulars[: 255 - len(late)] + late,
                source_id=line.fingerprint,
                **common,
            )
        else:
            entry = cash.withdraw(
                line.debit,
                reason=line.particulars[: 255 - len(late)] + late,
                sweep=kind == "sweep",
                source_type=cash.OWNER_EXPENSE if kind == "owner_expense" else "withdrawal",
                source_id=line.fingerprint,
                **common,
            )
    else:
        raise ReconcileError(f"{kind!r} lines are matched, not confirmed.")

    link(line, entry)
    return entry


#: Which entries a queued line of each proposed kind may be matched to by hand.
_MATCHABLE = {
    "contribution": [EntryKind.CONTRIBUTION],
    "withdrawal": [EntryKind.WITHDRAWAL],
    "sweep": [EntryKind.WITHDRAWAL],
    "owner_expense": [EntryKind.WITHDRAWAL],
    "account_charge": [EntryKind.ACCOUNT_CHARGE],
}
#: How far either side of the line's date a hand match may look (I7).
MATCH_WINDOW = timedelta(days=7)


def candidates_for(line: StatementLine) -> list[JournalEntry]:
    """Entries a person may match *line* to by hand: the proposed kind (any of
    them for a line the classifier cannot place), standing, unclaimed, within
    seven days either side, and moving Zerodha's cash by exactly the line's
    amount. Settlement and DP lines match automatically and get none."""
    proposal = propose(line)
    if proposal.kind in ("settlement", "dp_charge"):
        return []
    kinds = _MATCHABLE.get(proposal.kind) or sorted({k for ks in _MATCHABLE.values() for k in ks})
    entries = _unclaimed(
        JournalEntry.objects.filter(
            book=Book.LIVE,
            reversed_by__isnull=True,
            kind__in=kinds,
            occurred_on__range=(
                line.posting_date - MATCH_WINDOW,
                line.posting_date + MATCH_WINDOW,
            ),
        )
    ).order_by("occurred_on", "number")
    return [entry for entry in entries if _cash_effect(entry) == line.amount]


@transaction.atomic
def link_to(line: StatementLine, entry: JournalEntry, *, actor: str) -> JournalEntry:
    """Match a queued line to an entry already in the books, by hand (I7).

    For a line auto-matching could not place -- a UPI reference that is not
    the one typed, two equal lumpsums close together. It posts nothing: the
    entry already moved the money, so the cash effect is the same as if the
    line had matched on the way in. Audited, since a person chose it.
    """
    from audit.events import record
    from journal.posting import lock_book

    # Serialise with every live post and with another hand match, so the
    # entry cannot be claimed or reversed between the check and the link.
    lock_book(Book.LIVE)
    line.refresh_from_db()
    if _standing(line) is not None:
        raise ReconcileError("That line is already matched.")
    if entry.pk not in {candidate.pk for candidate in candidates_for(line)}:
        raise ReconcileError(
            f"Entry #{entry.number} cannot explain this line: it must be the line's kind, "
            "unreversed, not matched to another line, within seven days, and for the "
            "same amount."
        )
    link(line, entry)
    record(
        "journal.statement_matched",
        actor=actor,
        entity=line,
        after={
            "entry": str(entry.id),
            "entry_number": entry.number,
            "posting_date": str(line.posting_date),
            "amount": str(line.amount),
        },
    )
    return entry


@dataclass(frozen=True)
class Summary:
    """One upload against the books, as the CA ties it out (spec §8, I9)."""

    upload: StatementUpload
    closing_balance: Decimal
    ours: Decimal | None
    difference: Decimal | None
    matched: int
    by_hand: int
    confirmed: int
    opening: int
    queued: int


def summary(upload: StatementUpload) -> Summary:
    """Zerodha's closing balance on *upload* against the live book's Zerodha
    cash (1100 + 1110 + 1200 + 1210) through its last date, and how each of
    its lines is explained: matched on the way in, matched by hand, confirmed
    by hand (a posted entry), explained by the opening balance, or waiting.

    The balance is compared only for an upload ending on or after the opening
    date: before it the live book holds a fragment of Zerodha's cash, and the
    statement page compares the chain with the opening itself instead
    (`opening_disagreement`).
    """
    from audit.models import AuditEvent
    from journal.balances import zerodha_cash
    from journal.external import standing_opening

    opening = standing_opening()
    through = upload.period_to
    ours = difference = None
    if opening is not None and through is not None and through >= opening.occurred_on:
        ours = zerodha_cash(Book.LIVE, through=through)
        difference = q6(upload.closing_balance - ours)
    hand_matched = {
        (event.entity_id, (event.after or {}).get("entry"))
        for event in AuditEvent.objects.filter(
            action="journal.statement_matched",
            entity_type=StatementLine._meta.label,
            entity_id__in=[str(pk) for pk in upload.lines.values_list("pk", flat=True)],
        )
    }
    counts = dict.fromkeys(("matched", "by_hand", "confirmed", "opening", "queued"), 0)
    for line in upload.lines.select_related("matched_entry"):
        entry = _standing(line)
        if entry is None:
            counts["queued"] += 1
        elif entry.kind == EntryKind.OPENING_BALANCE:
            counts["opening"] += 1
        elif (entry.idempotency_key or "").startswith(f"statement:{line.fingerprint}"):
            counts["confirmed"] += 1
        elif (str(line.pk), str(entry.id)) in hand_matched:
            counts["by_hand"] += 1
        else:
            counts["matched"] += 1
    return Summary(
        upload=upload,
        closing_balance=q6(upload.closing_balance),
        ours=ours,
        difference=difference,
        **counts,
    )


def _fill_entries(day):
    """The date's live fill entries: our trades, not external ones, which settle
    outside the engine's net obligation."""
    return JournalEntry.objects.filter(
        book=Book.LIVE,
        kind__in=[EntryKind.BUY_FILL, EntryKind.SELL_FILL],
        source_type="fill",
        occurred_on=day,
    )


def _dp_on(day) -> Decimal:
    """The DP charges our sells on *day* carry, which Zerodha's settlement does not."""
    amounts = JournalLine.objects.filter(
        entry__in=_fill_entries(day).filter(kind=EntryKind.SELL_FILL),
        component=Component.DP,
    ).values_list("amount", flat=True)
    return q6(sum(amounts, ZERO))


def _corrections_for(line: StatementLine, day):
    """Every charge correction already made to the date's trades.

    The line's own corrections, and any other -- a contract note finalised
    before the statement arrived -- on one of the date's fills or on one of
    their lots, original or replacement. Zerodha's settlement already carries
    the true charges, so a correction made elsewhere is part of what we owe
    it, and leaving it out would post the same difference twice.
    """
    from django.db.models import Q

    from portfolio.models import Lot

    fill_ids = [str(i) for i in _fill_entries(day).values_list("source_id", flat=True)]
    lots = set(Lot.objects.filter(created_from_fill_id__in=fill_ids).values_list("id", flat=True))
    frontier = lots
    while frontier:
        frontier = (
            set(
                Lot.objects.filter(pk__in=frontier, superseded_by__isnull=False).values_list(
                    "superseded_by_id", flat=True
                )
            )
            - lots
        )
        lots |= frontier
    return JournalEntry.objects.filter(book=Book.LIVE).filter(
        Q(idempotency_key__startswith=_correction_prefix(line))
        | Q(kind=EntryKind.CHARGE_CORRECTION, source_type="fill", source_id__in=fill_ids)
        | Q(
            kind=EntryKind.CHARGE_CORRECTION,
            source_type="lot",
            source_id__in=[str(i) for i in lots],
        )
    )


def _correction_prefix(line: StatementLine) -> str:
    return f"settlement_correction:{line.fingerprint}:"


def settlement_difference(line: StatementLine) -> Decimal:
    """Zerodha's settlement less ours, counting corrections already made for it.

    Negative means Zerodha took more than we estimated. Our DP components for
    the date are added back, because Zerodha debits DP as a line of its own.
    """
    entry = line.matched_entry
    if entry is None or entry.kind != EntryKind.SETTLEMENT:
        raise ReconcileError("Only a matched settlement line has a difference.")
    day = entry.occurred_on
    settlements = JournalEntry.objects.filter(
        book=Book.LIVE, kind=EntryKind.SETTLEMENT, occurred_on=day
    )
    corrections = _corrections_for(line, day)
    ours = q6(sum((_cash_effect(e) for e in [*settlements, *corrections]), ZERO))
    return q6(line.amount - (ours + _dp_on(day)))


def disagreeing_settlements() -> list[dict]:
    """Matched settlement lines whose difference from ours is not zero.

    Each row carries the line, its difference, the correction bound and
    whether a correction is allowed. Computed defensively: a matched line that
    is not a settlement raises from `settlement_difference`, and is skipped.
    """
    rows = []
    matched = StatementLine.objects.filter(
        matched_entry__isnull=False,
        matched_entry__kind=EntryKind.SETTLEMENT,
        matched_entry__reversed_by__isnull=True,
    ).select_related("matched_entry")
    for line in matched:
        try:
            difference = settlement_difference(line)
        except ReconcileError:
            continue
        if difference != ZERO and _explanation_of(line, difference) is None:
            bound = correction_bound(line)
            rows.append(
                {
                    "line": line,
                    "difference": difference,
                    "bound": bound,
                    "correctable": abs(difference) <= bound,
                }
            )
    return rows


def _explanation_of(line: StatementLine, difference: Decimal):
    """The latest explanation of *line* that holds for *difference*, if any.

    An explanation is of the difference a person saw. If the line's
    difference moves afterwards -- a fill booked late, a correction -- it no
    longer covers it, and the line disagrees again until explained again.
    """
    return line.explanations.filter(difference=q6(difference)).order_by("-explained_at").first()


def explained_settlements() -> list[dict]:
    """Matched settlement lines that disagree, and a person has explained (m-4)."""
    rows = []
    matched = StatementLine.objects.filter(
        matched_entry__isnull=False,
        matched_entry__kind=EntryKind.SETTLEMENT,
        matched_entry__reversed_by__isnull=True,
        explanations__isnull=False,
    ).distinct()
    for line in matched.select_related("matched_entry"):
        try:
            difference = settlement_difference(line)
        except ReconcileError:
            continue
        explanation = _explanation_of(line, difference) if difference != ZERO else None
        if explanation is not None:
            rows.append({"line": line, "difference": difference, "explanation": explanation})
    return rows


@transaction.atomic
def explain_settlement(line: StatementLine, *, reason: str, actor: str):
    """Record a person's explanation of a settlement difference (m-4).

    For a difference no correction can take -- beyond what rounding on the
    day's charges explains, on a date that can no longer import a trade --
    once it has been reconciled by hand (OPERATIONS.md). Appended and
    audited; nothing is posted and the line is not changed. The year close
    and the page then stop counting it as disagreeing, for this difference.
    """
    from audit.context import current_trace_id
    from audit.events import record
    from journal.models import SettlementExplanation

    reason = (reason or "").strip()
    if not reason:
        raise ReconcileError("An explanation needs its reason: what was found, and where.")
    # Serialise with a correction of the same line, so the difference read
    # here is the one the explanation is written against.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_CORRECT_LOCK])
    line.refresh_from_db()
    difference = settlement_difference(line)
    if difference == ZERO:
        raise ReconcileError("That settlement agrees with ours: there is nothing to explain.")

    explanation = SettlementExplanation.objects.create(
        line=line,
        difference=difference,
        reason=reason[:500],
        explained_by=actor,
        trace_id=current_trace_id(),
    )
    record(
        "journal.settlement_explained",
        actor=actor,
        entity=line,
        after={
            "posting_date": str(line.posting_date),
            "difference": str(difference),
            "reason": reason[:500],
            "explanation": explanation.pk,
        },
    )
    return explanation


#: What rounding on a day's charges can plausibly come to (I5): five rupees,
#: or a tenth of a per cent of the day's turnover once that is more.
BOUND_FLOOR = Decimal("5")
BOUND_RATE = Decimal("0.001")


def _turnover(day) -> Decimal:
    from orders.models import Fill

    fill_ids = list(_fill_entries(day).values_list("source_id", flat=True))
    fills = Fill.objects.filter(pk__in=fill_ids)
    return q6(sum((fill.price * fill.quantity for fill in fills), ZERO))


def correction_bound(line: StatementLine) -> Decimal:
    """The largest settlement difference `correct_settlement` will book as charges.

    Charges are the rate card applied to the day's trades; what differs is the
    broker's rounding. A difference beyond this is something else -- a fill
    never recorded, a sale, a line matched to the wrong date -- and booking it
    as charges would bury it in lot cost or realised gain.
    """
    entry = line.matched_entry
    if entry is None or entry.kind != EntryKind.SETTLEMENT:
        raise ReconcileError("Only a matched settlement line has a difference.")
    return q6(max(BOUND_FLOOR, _turnover(entry.occurred_on) * BOUND_RATE))


_CORRECT_LOCK = 7_310_442_901_115_013


@transaction.atomic
def correct_settlement(line: StatementLine, *, actor: str) -> list[JournalEntry]:
    """Spread a settlement difference across the date's fills by turnover (§7.3).

    A buy's share goes to its lot through `contract_note.finalise`, so the lot
    is superseded at its final basis; a lot already final takes it as a charge
    correction. A sell's share adjusts its realised gain. Every entry is keyed
    by the line, so the difference then reads zero and a second call posts
    nothing. Never called automatically: a person presses the button.
    """
    from journal.trades import post_charge_correction
    from orders.models import Fill
    from portfolio.contract_note import FinalisationRefused, estimated_charges, finalise

    # Serialise: two concurrent runs would both see the full difference, and
    # the second's finalise would be refused into a fresh correction.
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_CORRECT_LOCK])

    line.refresh_from_db()
    difference = settlement_difference(line)
    if difference == ZERO:
        return []
    bound = correction_bound(line)
    if abs(difference) > bound:
        raise ReconcileError(
            f"A difference of ₹{difference:.2f} is more than rounding on the day's charges "
            f"can explain (at most ₹{bound:.2f}). Look for a missing fill, a sale, or a "
            "line matched to the wrong date before correcting anything."
        )
    extra = -difference  # positive: Zerodha charged more than we estimated
    day = line.matched_entry.occurred_on
    today = today_ist()  # a late fact is dated when it is known (§6)

    fill_entries = list(_fill_entries(day).order_by("number"))
    if not fill_entries:
        raise ReconcileError(f"No fills on {day} to carry a difference of {difference}.")

    fills = {
        str(fill.id): fill
        for fill in Fill.objects.select_related("order").filter(
            pk__in=[entry.source_id for entry in fill_entries]
        )
    }
    turnover = {
        entry.source_id: q6(fills[entry.source_id].price * fills[entry.source_id].quantity)
        for entry in fill_entries
    }
    whole = sum(turnover.values(), ZERO)
    reference = f"settlement {line.fingerprint[:12]}"
    prefix = _correction_prefix(line)

    posted: list[JournalEntry | None] = []
    remaining = extra
    for index, entry in enumerate(fill_entries):
        if index == len(fill_entries) - 1:
            share = remaining
        else:
            share = q6(extra * turnover[entry.source_id] / whole)
        remaining -= share
        if share == ZERO:
            continue
        fill = fills[entry.source_id]

        if entry.kind == EntryKind.BUY_FILL:
            lot = getattr(fill, "lot", None)
            if lot is None:
                raise ReconcileError(f"The buy fill {fill.id} has no lot to carry its share.")
            while lot.superseded_by_id is not None:
                lot = lot.superseded_by
            key = f"{prefix}{lot.id}"
            try:
                finalise(
                    lot,
                    actual_charges=estimated_charges(lot) + share,
                    actor=actor,
                    note_reference=reference,
                    correction_key=key,
                )
                posted.append(JournalEntry.objects.filter(idempotency_key=key).first())
            except FinalisationRefused:
                posted.append(
                    post_charge_correction(
                        lot, share, actor=actor, reference=reference, on=today, key=key
                    )
                )
        else:
            posted.append(
                post(
                    book=Book.LIVE,
                    kind=EntryKind.CHARGE_CORRECTION,
                    occurred_on=today,
                    description=f"sale charges on {day} settled against {reference}",
                    lines=[
                        Line(REALISED, share, fill.order.instrument_id, Component.OTHER),
                        Line(UNALLOCATED, -share),
                    ],
                    actor=actor,
                    source_type="fill",
                    source_id=str(fill.id),
                    idempotency_key=f"{prefix}{fill.id}",
                )
            )
    return [entry for entry in posted if entry is not None]
