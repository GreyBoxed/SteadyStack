"""Forms for the books: reversing an entry, and closing a financial year."""

import uuid

from django import forms
from django.core.validators import RegexValidator


def _date_widget() -> forms.DateInput:
    """A native date picker. The value must be ISO: a browser drops anything
    else from a date field silently, and the locale's own format is not ISO."""
    return forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")


class ReverseForm(forms.Form):
    reason = forms.CharField(
        max_length=200, help_text="Enough for somebody reading this in five years."
    )


class CloseYearForm(forms.Form):
    book = forms.ChoiceField(choices=[("live", "Live"), ("paper", "Paper")])
    fy_end = forms.DateField(widget=_date_widget(), help_text="31 March of the year closed.")


#: The kinds this form can confirm a line as. `settlement` and `dp_charge`
#: match automatically (`reconcile.auto_match`) and never reach this form;
#: anything else `classify.propose` returns -- `unknown` above all -- has no
#: honest preselection, so the widget offers an empty placeholder instead of
#: silently defaulting to the first real option.
CONFIRMABLE_KINDS = [
    ("contribution", "Money in from the owner"),
    ("withdrawal", "Money out to the owner"),
    ("sweep", "Quarterly settlement (carry owed)"),
    ("owner_expense", "The owner's cost (Kite Connect)"),
    ("account_charge", "An account charge (the portfolio's cost)"),
]


class ConfirmLineForm(forms.Form):
    kind = forms.ChoiceField(choices=CONFIRMABLE_KINDS)
    component = forms.ChoiceField(
        required=False,
        choices=[
            ("", "—"),
            ("amc", "AMC"),
            ("ddpi", "DDPI"),
            ("interest", "Interest"),
            ("dp", "DP charge"),
            ("other", "Other"),
        ],
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Added to the widget only, not to the field's own `choices`: the
        # placeholder is something to look at, never something to submit, so
        # `kind`'s validation still refuses an empty or unrecognised value.
        self.fields["kind"].widget.choices = [("", "— choose —"), *CONFIRMABLE_KINDS]


class DividendForm(forms.Form):
    instrument = forms.ModelChoiceField(queryset=None, to_field_name="tradingsymbol")
    amount = forms.DecimalField(max_digits=14, decimal_places=2, min_value=0.01)
    paid_on = forms.DateField(widget=_date_widget())
    reference = forms.CharField(max_length=100, required=False)
    #: One rendering of this form, not one dividend: filled with a fresh
    #: value on every GET, so a double-submit of that rendering (a slow
    #: network, an impatient second click) posts once, while two dividends
    #: that genuinely share every visible field still both post. Required and
    #: shaped like `uuid4().hex` on purpose -- a client-chosen string (e.g. the
    #: same "not-a-uuid" sent for two real dividends) would reopen the exact
    #: collision this field exists to prevent.
    submission = forms.CharField(
        widget=forms.HiddenInput,
        validators=[RegexValidator(r"^[0-9a-f]{32}$")],
    )

    def __init__(self, *args, **kwargs):
        from portfolio.instruments import ever_held

        super().__init__(*args, **kwargs)
        # Only what the account has held: a name never held pays it nothing,
        # and one outside the list is refused as an invalid choice.
        self.fields["instrument"].queryset = ever_held()
        if not self.is_bound:
            self.initial["submission"] = uuid.uuid4().hex

    def clean_paid_on(self):
        from audit.context import today_ist

        paid_on = self.cleaned_data["paid_on"]
        if paid_on > today_ist():
            raise forms.ValidationError("A dividend cannot have been paid on a day still to come.")
        return paid_on
