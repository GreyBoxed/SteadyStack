"""Operator overrides on a day's tradesheet.

Thin on purpose. Every rule these express is enforced in `orders.overrides` --
a skip is refused once anything has filled, a reduction cancels the newest
orders first -- and a form that re-checked them would be a second opinion
that could disagree with the first.
"""

from django import forms


class ReduceForm(forms.Form):
    quantity = forms.IntegerField(
        min_value=0,
        help_text="The new quantity for the day. Newest open orders are cancelled to reach it.",
    )
    reason = forms.CharField(max_length=255, help_text="Recorded with the override.")


class AdjustLineForm(forms.Form):
    """One name's quantity: lower cancels its newest orders, higher needs the cash."""

    symbol = forms.CharField(max_length=64)
    quantity = forms.IntegerField(min_value=0)
    reason = forms.CharField(max_length=255)


class SkipLineForm(forms.Form):
    """Stop buying one name today; what has filled of it stands."""

    symbol = forms.CharField(max_length=64)
    reason = forms.CharField(max_length=255)


class ExcludeTickForm(forms.Form):
    tick_no = forms.IntegerField(
        min_value=0,
        help_text="This tick places nothing. The day's quantity is untouched.",
    )
    reason = forms.CharField(max_length=255)


class SkipForm(forms.Form):
    reason = forms.CharField(
        max_length=255,
        help_text="Refused once anything has filled — a day with fills is partial, not skipped.",
    )
