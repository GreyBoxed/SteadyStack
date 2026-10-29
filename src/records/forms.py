"""Classifying a divergence, and recording what explains it.

A classification on its own explains nothing: saying "sold outside" does not
consume the lots, and the holding stays wrong until something does. So the
form offers to record the movement in the same step, and the store links the
two, which is what `classify(explained_by=...)` exists for.
"""

import uuid

from django import forms
from django.core.validators import RegexValidator

from records.models import DivergenceClassification, ExternalSide, PaidFrom


def _today():
    from audit.context import today_ist

    return today_ist()


def _date_widget() -> forms.DateInput:
    """A native date picker. The value must be ISO: a browser drops anything
    else from a date field silently, and the locale's own format is not ISO."""
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


#: What each classification needs before it explains anything.
NEEDS_ENTRY = {
    DivergenceClassification.EXTERNAL_BUY: ExternalSide.BUY,
    DivergenceClassification.EXTERNAL_SELL: ExternalSide.SELL,
}


class ClassifyForm(forms.Form):
    classification = forms.ChoiceField(
        choices=[
            (value, label)
            for value, label in DivergenceClassification.choices
            if value != DivergenceClassification.UNCLASSIFIED
        ],
        help_text="What the difference actually was.",
    )
    paid_from = forms.ChoiceField(
        choices=PaidFrom.choices,
        initial=PaidFrom.ZERODHA,
        required=False,
        help_text="An external buy paid from the bank -- an IPO through UPI -- is a "
        "contribution in shares, not a movement of Zerodha's own funds.",
    )
    note = forms.CharField(
        max_length=255,
        required=True,
        help_text="Enough for somebody reading this in five years.",
    )

    price = forms.DecimalField(
        max_digits=14,
        decimal_places=4,
        required=False,
        help_text="Per share. Needed when an external buy or sell explains it.",
    )
    charges = forms.DecimalField(max_digits=12, decimal_places=4, required=False, initial=0)
    occurred_on = forms.DateField(
        required=False,
        widget=_date_widget(),
        help_text="When it happened at the broker.",
    )

    def clean(self):
        cleaned = super().clean()
        side = NEEDS_ENTRY.get(cleaned.get("classification"))
        if side is None:
            return cleaned

        if cleaned.get("price") in (None, ""):
            self.add_error(
                "price",
                "An external movement needs its price, or the lots it creates have no "
                "cost basis and every gain after it is wrong.",
            )
        if not cleaned.get("occurred_on"):
            self.add_error("occurred_on", "An external movement needs the date it happened.")
        return cleaned

    @property
    def side(self) -> str | None:
        return NEEDS_ENTRY.get(self.cleaned_data.get("classification"))


class ContractNoteForm(forms.Form):
    """What the note actually charged, against what was estimated at fill."""

    actual_charges = forms.DecimalField(
        max_digits=12,
        decimal_places=4,
        min_value=0,
        help_text="Total charges from the contract note for this lot.",
    )
    note_reference = forms.CharField(
        max_length=64,
        required=False,
        help_text="The note's own reference, so this can be found again.",
    )


class CorporateActionForm(forms.Form):
    """A ratio applied to every open lot of one instrument.

    Entered by a person on purpose: a split detected automatically and applied
    without anybody looking is a silent rewrite of every cost basis in the name.
    """

    instrument = forms.CharField(max_length=64, help_text="Trading symbol.")
    new_shares = forms.IntegerField(
        min_value=1, help_text="5-for-1 split is 5 new shares per 1 old."
    )
    per_old_shares = forms.IntegerField(
        min_value=1, help_text="A 1-for-10 consolidation is 1 new per 10 old."
    )
    effective_on = forms.DateField(widget=_date_widget())
    reason = forms.CharField(max_length=255)


class LumpsumForm(forms.Form):
    """An ad-hoc contribution (M8a)."""

    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0)
    # Usually today, so it starts there; the calendar is for the other cases.
    received_on = forms.DateField(
        widget=_date_widget(),
        initial=_today,
    )
    # The label is what the page shows (the form renders no help text): the
    # statement matches a lumpsum by this number (spec §8), so ask for it.
    reference = forms.CharField(
        label="UPI reference",
        max_length=255,
        required=False,
        help_text=(
            "The UPI reference on the bank transfer, so Zerodha's statement line "
            "can be matched to it."
        ),
    )
    #: One rendering of this form, not one lumpsum -- the dividend form's
    #: pattern (`journal.forms.DividendForm.submission`): a double submit of
    #: the same rendering posts once, two real lumpsums still both post.
    submission = forms.CharField(
        widget=forms.HiddenInput,
        validators=[RegexValidator(r"^[0-9a-f]{32}$")],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["submission"] = uuid.uuid4().hex

    def clean_received_on(self):
        received_on = self.cleaned_data["received_on"]
        if received_on > _today():
            raise forms.ValidationError("Money cannot have arrived on a day still to come.")
        return received_on
