"""The settings form.

One form over the store's mutable fields, grouped the way the operator thinks
about them rather than the way the model lists them: what to invest, when to
act, and who we are to the broker.

Nothing here writes. `store.update` does that, so a change made from a browser
takes exactly the same audited path as one made from a shell, and the live
gate refuses in one place rather than two.
"""

from django import forms

from configuration.models import ConfigurationVersion
from configuration.store import MUTABLE_FIELDS

#: Field name to the group it belongs to on the page. Anything absent is
#: still editable, it just sorts into "Other" -- adding a field to the model
#: must never silently drop it from the screen.
GROUPS = {
    "Investment": [
        "daily_amount",
        "max_price_per_share",
        "slice_quantity",
        "reserve_threshold_days",
    ],
    "Clock": [
        "tradesheet_time",
        "placer_interval_seconds",
        "placement_window_close",
        "market_fallback_time",
        "close_window_end",
    ],
    "Pricing": [
        "catch_up_minutes",
        "order_ttl_minutes",
        "ladder_interval_minutes",
        "ladder_patience",
    ],
    "Broker": [
        "order_mode",
        "registered_static_ip",
        "fallback_band_pct",
        "max_orders_per_window",
        "order_rate_window_seconds",
    ],
    "Reporting": ["benchmark_symbol", "market_data_monthly_cost"],
}


class ConfigurationForm(forms.ModelForm):
    class Meta:
        model = ConfigurationVersion
        fields = sorted(MUTABLE_FIELDS)

    reason = forms.CharField(
        max_length=255,
        required=True,
        help_text="Why this changed. It is stored with the version and cannot be edited after.",
    )

    def grouped(self):
        """Yield (group, [bound fields]) in the page's order."""
        placed = {name for names in GROUPS.values() for name in names}
        for group, names in GROUPS.items():
            yield group, [self[name] for name in names if name in self.fields]

        leftover = [self[name] for name in self.fields if name not in placed and name != "reason"]
        if leftover:
            yield "Other", leftover

    def changes(self, before: dict) -> dict:
        """Only the fields that actually differ from *before*.

        The store appends a version per call, so sending every field would
        record a change on each save and bury the one that mattered.

        `before` is a snapshot taken before validation, deliberately: a
        ModelForm writes its cleaned values onto `instance` during
        `is_valid()`, so comparing against the instance afterwards finds every
        field equal and reports that nothing changed.
        """
        changed = {}
        for name in sorted(MUTABLE_FIELDS):
            if name not in self.cleaned_data:
                continue
            if not _same(self.cleaned_data[name], before.get(name)):
                changed[name] = self.cleaned_data[name]
        return changed

    @staticmethod
    def snapshot(config: ConfigurationVersion) -> dict:
        return {name: getattr(config, name) for name in MUTABLE_FIELDS}


def _same(new, old) -> bool:
    """Whether two field values mean the same thing.

    An empty nullable field cleans to "" while the column holds None. Treating
    those as different would record a change on every save that touched
    nothing, which is exactly the noise the version history exists to avoid.
    """
    blank = (None, "")
    if new in blank and old in blank:
        return True
    return new == old
