"""The books a chartered accountant reads: trial balance, journal, ledgers.

Every figure on these screens is a sum of journal lines -- nothing here is
typed in, and the debit and credit totals on the trial balance page are the
sole evidence that the ledger still balances. A CSV export carries the same
rows the screen does, at the journal's own six decimal places rather than the
two a screen rounds to for display (spec: money is quantised for display
only, never for storage).
"""

import logging
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from audit.context import today_ist
from journal.balances import BROUGHT_FORWARD, general_ledger, trial_balance
from journal.checks import TOLERANCE
from journal.external import record_dividend
from journal.forms import (
    CONFIRMABLE_KINDS,
    CloseYearForm,
    ConfirmLineForm,
    DividendForm,
    ReverseForm,
)
from journal.models import (
    Account,
    BalanceCheck,
    Book,
    EntryKind,
    JournalEntry,
    JournalLine,
    PeriodClose,
    StatementLine,
    StatementUpload,
)
from journal.periods import JournalPeriodError, close_year, fy_end_for
from journal.posting import JournalError
from journal.posting import reverse as reverse_the_entry
from journal.statement import reconcile
from journal.statement.classify import propose
from journal.statement.parse import StatementError
from journal.statement.reconcile import ReconcileError
from journal.statement.store import store_upload

logger = logging.getLogger("steadystack.journal")

ZERO = Decimal("0")
PAGE_SIZE = 100
#: A Console statement is a few dozen lines of a bank's worth of text; a file
#: this much larger than that is not one, mirroring rebalance/views.py's guard.
MAX_STATEMENT_BYTES = 1024 * 1024


def _csv(name: str, header: list[str], rows: list[list]) -> HttpResponse:
    """A download of exactly what the page shows, for a spreadsheet or an auditor."""
    import csv

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}-{today_ist():%Y-%m-%d}.csv"'
    writer = csv.writer(response)
    writer.writerow(header)
    # A text cell starting = + - @ is a formula to a spreadsheet; the quote
    # makes it text. Descriptions carry statement particulars and typed
    # reasons. Numbers are not text, so a negative amount stays a number.
    writer.writerows(
        [
            [
                f"'{cell}" if isinstance(cell, str) and cell[:1] in ("=", "+", "-", "@") else cell
                for cell in row
            ]
            for row in rows
        ]
    )
    return response


def _date_arg(request: HttpRequest, name: str) -> date | None:
    """A range bound the URL asked for, or None.

    A malformed date is treated as absent rather than as an error: this is a
    reading surface, and a typo in a query string should not take the page
    away from the person trying to use it.
    """
    raw = request.GET.get(name, "")
    try:
        return date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _book(request: HttpRequest) -> str:
    """The live book unless the paper one is asked for by name (spec §3).

    Not the order mode: while paper-trading that would open the CA's pages on
    the paper book, and nothing the CA is shown reads the paper book.
    """
    asked = request.GET.get("book")
    return asked if asked in Book.values else Book.LIVE


def _earliest(book: str) -> date:
    """The first day this book has an entry on, so a ledger's default window
    opens at zero rather than at an arbitrary date nothing precedes."""
    first = (
        JournalEntry.objects.filter(book=book)
        .order_by("occurred_on")
        .values_list("occurred_on", flat=True)
        .first()
    )
    return first or today_ist()


def _split(amount: Decimal) -> tuple[Decimal, Decimal]:
    """A signed amount, as the debit and credit columns a CA expects."""
    return (amount, ZERO) if amount > ZERO else (ZERO, -amount)


@require_http_methods(["GET"])
def index(request: HttpRequest) -> HttpResponse:
    """The trial balance: every account's closing balance, on the side it falls.

    Zerodha's cash is 1100 + 1110 + 1200 + 1210. The engine may spend 1110 +
    1200. Every figure here is a sum of journal lines; nothing is typed in.
    """
    book = _book(request)
    today = today_ist()
    fy = _date_arg(request, "fy")
    if fy is not None and (fy.month, fy.day) == (3, 31):
        # A whole financial year, or the running one up to today.
        through = min(fy, today)
    else:
        through = _date_arg(request, "to") or today
    # Income and expense from the start of *through*'s financial year, with
    # what came before brought forward on one line (M11).
    start = date(fy_end_for(through).year - 1, 4, 1)

    rows = trial_balance(book=book, start=start, through=through)

    if request.GET.get("format") == "csv":
        return _csv(
            "trial-balance",
            ["code", "name", "type", "debit", "credit"],
            [[row.code, row.name, row.type, row.debit, row.credit] for row in rows],
        )

    # The FY just before the current one -- almost always the one a CA is
    # ready to sign off. 31 March always exists, so no date arithmetic needed.
    current_fy_end = fy_end_for(today)
    suggested_close = date(current_fy_end.year - 1, 3, 31)
    first_fy_end = fy_end_for(_earliest(book))
    years = [
        {"end": date(year, 3, 31), "label": f"FY {year - 1}-{str(year)[2:]}"}
        for year in range(current_fy_end.year, first_fy_end.year - 1, -1)
    ]

    return render(
        request,
        "journal/index.html",
        {
            "book": book,
            "through": through,
            "start": start,
            "years": years,
            "selected_fy": fy_end_for(through),
            "rows": rows,
            "debit_total": sum((row.debit for row in rows), ZERO),
            "credit_total": sum((row.credit for row in rows), ZERO),
            "brought_forward": BROUGHT_FORWARD,
            "closed_years": PeriodClose.objects.filter(book=book).order_by("-fy_end"),
            "close_form": CloseYearForm(initial={"book": book, "fy_end": suggested_close}),
            "balance_checks": [
                {
                    "checked_on": row.checked_on,
                    "ours": row.ours,
                    "theirs": row.theirs,
                    "difference": row.difference,
                    "disagrees": abs(row.difference) > TOLERANCE,
                }
                for row in BalanceCheck.objects.all()[:5]
            ],
        },
    )


@require_http_methods(["GET"])
def entries(request: HttpRequest) -> HttpResponse:
    """Every entry in range, newest first -- the journal itself."""
    book = _book(request)
    start = _date_arg(request, "from")
    end = _date_arg(request, "to")

    query = JournalEntry.objects.filter(book=book)
    if start:
        query = query.filter(occurred_on__gte=start)
    if end:
        query = query.filter(occurred_on__lte=end)
    query = query.order_by("-occurred_on", "-number")

    if request.GET.get("format") == "csv":
        lines = (
            JournalLine.objects.filter(entry__in=query)
            .select_related("entry", "account")
            .order_by("-entry__occurred_on", "-entry__number", "id")
        )
        rows = []
        for line in lines:
            debit, credit = _split(line.amount)
            rows.append(
                [
                    line.entry.number,
                    line.entry.occurred_on,
                    line.entry.kind,
                    line.entry.description,
                    line.account_id,
                    line.account.name,
                    line.instrument_id or "",
                    line.component,
                    debit,
                    credit,
                    line.entry.trace_id,
                ]
            )
        header = [
            "number", "date", "kind", "description", "account", "account_name",
            "instrument", "component", "debit", "credit", "trace_id",
        ]  # fmt: skip
        return _csv("journal", header, rows)

    page = Paginator(
        query.prefetch_related("lines__account", "lines__instrument"), PAGE_SIZE
    ).get_page(request.GET.get("page"))

    return render(
        request,
        "journal/journal.html",
        {"book": book, "start": start, "end": end, "page": page},
    )


@require_http_methods(["GET"])
def ledger(request: HttpRequest, code: str) -> HttpResponse:
    """One account's lines in a range, with the balance brought forward."""
    try:
        account = Account.objects.get(pk=code)
    except Account.DoesNotExist as exc:
        raise Http404("No such account.") from exc

    book = _book(request)
    start = _date_arg(request, "from") or _earliest(book)
    end = _date_arg(request, "to") or today_ist()

    opening, rows = general_ledger(code, book=book, start=start, end=end)

    if request.GET.get("format") == "csv":
        csv_rows = []
        for row in rows:
            debit, credit = _split(row.amount)
            csv_rows.append(
                [
                    row.entry.occurred_on,
                    row.entry.number,
                    row.entry.kind,
                    row.entry.description,
                    row.instrument_id or "",
                    row.component,
                    debit,
                    credit,
                    row.running,
                ]
            )
        header = [
            "date", "number", "kind", "description", "instrument", "component",
            "debit", "credit", "running",
        ]  # fmt: skip
        return _csv(f"ledger-{code}", header, csv_rows)

    return render(
        request,
        "journal/ledger.html",
        {
            "account": account,
            "book": book,
            "start": start,
            "end": end,
            "opening": opening,
            "rows": rows,
        },
    )


#: What `/books/` may reverse: entries a person recorded or confirmed. An
#: engine entry -- an allocation, a fill, a settlement, an external trade --
#: has lots, a cash ledger row or a record resting on it, and reversing only
#: the journal side would split the two.
REVERSIBLE_KINDS = (
    EntryKind.CONTRIBUTION,
    EntryKind.WITHDRAWAL,
    EntryKind.ACCOUNT_CHARGE,
    EntryKind.DIVIDEND,
    EntryKind.OPENING_BALANCE,
    EntryKind.CHARGE_CORRECTION,
)


def _entry_context(row: JournalEntry, *, form: ReverseForm | None = None) -> dict:
    try:
        reversed_by = row.reversed_by
    except JournalEntry.DoesNotExist:
        reversed_by = None

    return {
        "entry": row,
        "lines": row.lines.select_related("account", "instrument").all(),
        "reverses": row.reverses,
        "reversed_by": reversed_by,
        "statement_lines": row.statement_lines.all(),
        "reverse_form": form or ReverseForm(),
        "reversible": row.kind in REVERSIBLE_KINDS,
    }


@require_http_methods(["GET"])
def entry(request: HttpRequest, pk) -> HttpResponse:
    """One entry: its lines, what it reverses or is reversed by, and its
    matched statement lines -- everything a CA needs to trust one row."""
    row = get_object_or_404(JournalEntry, pk=pk)
    return render(request, "journal/entry.html", _entry_context(row))


@require_http_methods(["POST"])
def reverse_entry(request: HttpRequest, pk) -> HttpResponse:
    """Undo an entry by appending its mirror. Never a silent edit."""
    row = get_object_or_404(JournalEntry, pk=pk)
    if row.kind not in REVERSIBLE_KINDS:
        messages.error(
            request,
            f"A {row.get_kind_display().lower()} entry is the engine's, and is not reversed "
            "from the books: what rests on it would no longer agree.",
        )
        return redirect("journal:entry", pk=row.pk)
    form = ReverseForm(request.POST)

    if not form.is_valid():
        return render(request, "journal/entry.html", _entry_context(row, form=form))

    try:
        reverse_the_entry(
            row, reason=form.cleaned_data["reason"], actor=request.user.get_username()
        )
    except JournalError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Entry #{row.number} reversed.")

    return redirect("journal:entry", pk=row.pk)


@require_http_methods(["POST"])
def close(request: HttpRequest) -> HttpResponse:
    """Sign off a financial year, so nothing before it can post again."""
    form = CloseYearForm(request.POST)

    if not form.is_valid():
        messages.error(request, "Give a book and the 31 March the year ended.")
        return redirect("journal:index")

    try:
        close_year(
            book=form.cleaned_data["book"],
            fy_end=form.cleaned_data["fy_end"],
            actor=request.user.get_username(),
        )
    except JournalPeriodError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{form.cleaned_data['fy_end']} closed.")

    return redirect("journal:index")


def _settlements_csv(disagreeing: list[dict], explained: list[dict]) -> HttpResponse:
    """Every settlement line that differs from ours, for the CA (Nm6): its
    date, the difference, the correction bound, and -- where a person has
    reconciled it by hand -- who explained it, why and when."""
    from journal.statement.reconcile import correction_bound

    rows = [
        [
            row["line"].posting_date, row["line"].upload.filename, row["difference"],
            row["bound"], "disagreeing", "", "", "",
        ]
        for row in disagreeing
    ] + [
        [
            row["line"].posting_date, row["line"].upload.filename, row["difference"],
            correction_bound(row["line"]), "explained", row["explanation"].explained_by,
            row["explanation"].reason, row["explanation"].explained_at.isoformat(),
        ]
        for row in explained
    ]  # fmt: skip
    rows.sort(key=lambda row: (row[0], row[4]))
    return _csv(
        "statement-settlements",
        [
            "date", "filename", "difference", "bound", "state",
            "explained_by", "reason", "explained_at",
        ],
        rows,
    )  # fmt: skip


@require_http_methods(["GET", "POST"])
def statement(request: HttpRequest) -> HttpResponse:
    """Upload a Console statement, and work the queue it leaves behind.

    Every line either matches an entry already in the books or waits here.
    Nothing posts until a person confirms it.
    """
    if request.method == "POST":
        uploaded = request.FILES.get("file")
        if uploaded is None:
            messages.error(request, "Choose a file to upload.")
        elif uploaded.size > MAX_STATEMENT_BYTES:
            messages.error(request, "That file is far larger than a Console statement should be.")
        else:
            try:
                stored = store_upload(
                    uploaded.read(), filename=uploaded.name, actor=request.user.get_username()
                )
            except StatementError as exc:
                messages.error(request, str(exc))
            else:
                linked = reconcile.auto_match(stored.upload)
                messages.success(
                    request,
                    f"{stored.added} new lines, {linked} matched to entries already in the books",
                )
        return redirect("journal:statement")

    summaries = [reconcile.summary(upload) for upload in StatementUpload.objects.all()]
    disagreeing = reconcile.disagreeing_settlements()
    explained = reconcile.explained_settlements()
    if request.GET.get("format") == "csv":
        # Per upload, how many of its settlement lines still disagree and how
        # many a person has explained (Nm6): an explained line keeps its
        # difference for good, and the CA should see that it is there.
        def count(rows, upload):
            return sum(1 for row in rows if row["line"].upload_id == upload.pk)

        return _csv(
            "statement-reconciliation",
            [
                "uploaded_at", "filename", "period_from", "period_to", "closing_balance",
                "ours", "difference", "matched", "matched_by_hand", "confirmed",
                "by_the_opening", "queued", "disagreeing", "explained",
            ],
            [
                [
                    row.upload.uploaded_at.isoformat(), row.upload.filename,
                    row.upload.period_from, row.upload.period_to, row.closing_balance,
                    row.ours, row.difference, row.matched, row.by_hand, row.confirmed,
                    row.opening, row.queued, count(disagreeing, row.upload),
                    count(explained, row.upload),
                ]
                for row in summaries
            ],
        )  # fmt: skip
    if request.GET.get("format") == "settlements-csv":
        return _settlements_csv(disagreeing, explained)

    confirmable = dict(CONFIRMABLE_KINDS)
    queue = []
    for line in reconcile.queue():
        proposal = propose(line)
        if proposal.kind in ("settlement", "dp_charge"):
            # These match automatically once their trade lands; offering a
            # form here would let somebody confirm a line the system is
            # about to link on its own.
            queue.append({"line": line, "proposal": proposal, "form": None, "candidates": []})
            continue
        initial_kind = proposal.kind if proposal.kind in confirmable else ""
        queue.append(
            {
                "line": line,
                "proposal": proposal,
                "form": ConfirmLineForm(
                    initial={"kind": initial_kind, "component": proposal.component}
                ),
                "candidates": reconcile.candidates_for(line),
            }
        )

    return render(
        request,
        "journal/statement.html",
        {
            "summaries": summaries,
            "queue": queue,
            "settlement_lines": disagreeing,
            "explained_lines": explained,
            "opening_disagreement": reconcile.opening_disagreement(),
        },
    )


@require_http_methods(["POST"])
def confirm_line(request: HttpRequest, pk: int) -> HttpResponse:
    """Post the entry a person says one queued line is."""
    line = get_object_or_404(StatementLine, pk=pk)
    form = ConfirmLineForm(request.POST)

    if not form.is_valid():
        messages.error(request, "Choose what this line is.")
        return redirect("journal:statement")

    try:
        reconcile.confirm(
            line,
            kind=form.cleaned_data["kind"],
            component=form.cleaned_data["component"],
            actor=request.user.get_username(),
        )
    except (ReconcileError, JournalError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Line confirmed and posted.")

    return redirect("journal:statement")


@require_http_methods(["POST"])
def match_line(request: HttpRequest, pk: int) -> HttpResponse:
    """Match one queued line to an entry already in the books. Posts nothing."""
    line = get_object_or_404(StatementLine, pk=pk)
    try:
        chosen = JournalEntry.objects.get(pk=request.POST.get("entry", ""))
    except (JournalEntry.DoesNotExist, ValueError, ValidationError):
        messages.error(request, "Choose the entry this line is.")
        return redirect("journal:statement")

    try:
        reconcile.link_to(line, chosen, actor=request.user.get_username())
    except (ReconcileError, StatementError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Line matched to entry #{chosen.number}.")

    return redirect("journal:statement")


@require_http_methods(["POST"])
def correct(request: HttpRequest, pk: int) -> HttpResponse:
    """Spread a settlement's disagreement across the day's fills."""
    line = get_object_or_404(StatementLine, pk=pk)

    try:
        posted = reconcile.correct_settlement(line, actor=request.user.get_username())
    except (ReconcileError, JournalError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"{len(posted)} entr{'y' if len(posted) == 1 else 'ies'} posted.")

    return redirect("journal:statement")


@require_http_methods(["POST"])
def explain(request: HttpRequest, pk: int) -> HttpResponse:
    """Record why a settlement difference stands, once reconciled by hand (m-4)."""
    line = get_object_or_404(StatementLine, pk=pk)

    try:
        reconcile.explain_settlement(
            line, reason=request.POST.get("reason", ""), actor=request.user.get_username()
        )
    except ReconcileError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Explanation recorded. The settlement no longer disagrees.")

    return redirect("journal:statement")


@require_http_methods(["GET", "POST"])
def dividend(request: HttpRequest) -> HttpResponse:
    """The portfolio's income, paid to the owner's bank."""
    form = DividendForm(request.POST or None)

    if request.method == "POST":
        if form.is_valid():
            instrument = form.cleaned_data["instrument"]
            amount = form.cleaned_data["amount"]
            paid_on = form.cleaned_data["paid_on"]
            reference = form.cleaned_data["reference"]
            key = f"dividend:{form.cleaned_data['submission']}"

            try:
                record_dividend(
                    instrument.tradingsymbol,
                    amount,
                    paid_on=paid_on,
                    actor=request.user.get_username(),
                    reference=reference,
                    idempotency_key=key,
                )
            except (ValueError, JournalError) as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, "Recorded as the portfolio's income, paid to your bank.")
                return redirect("journal:dividend")
        elif "submission" in form.errors:
            # A missing or tampered hidden token, not a person's mistake --
            # nothing about the visible fields is wrong, so a generic banner
            # rather than a field error is what tells them what to do.
            messages.error(request, "Reload the form and try again.")

    return render(request, "journal/dividend.html", {"form": form})
