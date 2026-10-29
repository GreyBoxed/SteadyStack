"""Chart geometry, computed here and drawn by the template as inline SVG.

There is no charting library and no JavaScript. A chart on these pages is a
record like any other: it has to render with the network down and read the
same in a year, so the coordinates are worked out on the server and the
template only joins them into paths. Everything in this module is layout --
where a value lands on the page -- never a figure the operator reads as money.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class Scale:
    """A linear map from a value range onto a pixel range.

    `flip` is for the vertical axis, where larger values sit higher and SVG's
    y grows downward.
    """

    lo: float
    hi: float
    start: float
    end: float
    flip: bool = False

    def __call__(self, value) -> float:
        span = self.hi - self.lo
        ratio = 0.5 if span == 0 else (float(value) - self.lo) / span
        if self.flip:
            ratio = 1 - ratio
        return round(self.start + ratio * (self.end - self.start), 1)


def padded(lo: float, hi: float, pad: float = 0.08) -> tuple[float, float]:
    """A range with some air, so a line never sits on the frame."""
    if hi == lo:
        margin = abs(hi) * 0.01 or 1.0
        return lo - margin, hi + margin
    margin = (hi - lo) * pad
    return lo - margin, hi + margin


def nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    """Round-numbered gridlines inside a range."""
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / count
    magnitude = 10 ** len(str(int(raw))) / 10 if raw >= 1 else _small_magnitude(raw)
    for step in (1, 2, 2.5, 5, 10):
        size = step * magnitude
        if (hi - lo) / size <= count:
            break
    first = (int(lo / size) + (1 if lo > 0 else 0)) * size
    ticks = []
    value = first
    while value <= hi:
        if value >= lo:
            ticks.append(round(value, 6))
        value += size
    return ticks


def _small_magnitude(raw: float) -> float:
    magnitude = 1.0
    while magnitude > raw:
        magnitude /= 10
    return magnitude


def path(points: list[tuple[float, float]]) -> str:
    """An SVG path through the points, in order."""
    return " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(points))


def minutes(moment: datetime) -> float:
    """Minutes since midnight, the unit an intraday axis is laid out in."""
    return moment.hour * 60 + moment.minute + moment.second / 60


def hhmm(total: float) -> str:
    whole = int(total)
    return f"{whole // 60:02d}:{whole % 60:02d}"


@dataclass
class Series:
    """A value-over-time chart: one or more lines on shared axes."""

    width: int
    height: int
    lines: list[dict] = field(default_factory=list)
    area: str = ""
    grid: list[dict] = field(default_factory=list)
    x_labels: list[dict] = field(default_factory=list)
    empty: bool = False
    right: int = 0


def value_series(
    rows: list[dict],
    *,
    keys: list[str],
    width: int = 860,
    height: int = 240,
    fill_key: str | None = None,
    label=lambda v: f"{v:,.0f}",
    label_room: int = 150,
) -> Series:
    """Lines for each key across dated rows; the first key may be filled.

    `rows` carry a `date` and a Decimal (or None) under each key. A day with a
    missing value is a gap in that line rather than a zero, because a zero
    would draw a crash that never happened.
    """
    # `label_room` is kept free on the right for the lines' end labels.
    left, right, top, bottom = 64, width - label_room, 10, height - 26
    if len(rows) < 2:
        return Series(width=width, height=height, empty=True)

    values = [float(row[k]) for row in rows for k in keys if row.get(k) is not None]
    if not values:
        return Series(width=width, height=height, empty=True)

    lo, hi = padded(min(values), max(values))
    x = Scale(0, len(rows) - 1, left, right)
    y = Scale(lo, hi, top, bottom, flip=True)

    lines = []
    area = ""
    for key in keys:
        points = [(x(i), y(row[key])) for i, row in enumerate(rows) if row.get(key) is not None]
        lines.append(
            {
                "key": key,
                "d": path(points),
                "end_x": points[-1][0] if points else None,
                "end_y": points[-1][1] if points else None,
            }
        )
        if key == fill_key and points:
            area = (
                path(points)
                + f" L{points[-1][0]:.1f} {bottom:.1f} L{points[0][0]:.1f} {bottom:.1f} Z"
            )

    _spread_labels(lines, gap=14)

    grid = [{"y": y(v), "ty": round(y(v) + 3.5, 1), "label": label(v)} for v in nice_ticks(lo, hi)]

    return Series(
        width=width,
        height=height,
        lines=lines,
        area=area,
        grid=grid,
        x_labels=_date_labels(rows, x),
        right=right,
    )


def _spread_labels(lines: list[dict], *, gap: float) -> None:
    """Give each line's end label a y that does not overprint its neighbour."""
    placed = sorted(
        (line for line in lines if line["end_y"] is not None), key=lambda line: line["end_y"]
    )
    previous = None
    for line in placed:
        y = line["end_y"]
        if previous is not None and y - previous < gap:
            y = previous + gap
        line["label_y"] = round(y, 1)
        previous = y


def _date_labels(rows: list[dict], x: Scale, count: int = 6) -> list[dict]:
    n = len(rows)
    step = max(1, (n - 1) // (count - 1))
    picks = list(range(0, n, step))
    if picks[-1] != n - 1 and n - 1 - picks[-1] > step // 2:
        picks.append(n - 1)
    span_days = (rows[-1]["date"] - rows[0]["date"]).days
    fmt = "%b %y" if span_days > 200 else "%d %b"
    return [{"x": x(i), "label": _fmt_date(rows[i]["date"], fmt)} for i in picks]


def _fmt_date(day: date, fmt: str) -> str:
    return day.strftime(fmt).lstrip("0")


def to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def rupees_short(value: float) -> str:
    """An axis label in the units an Indian reader scans: thousands, lakhs, crores."""
    magnitude = abs(value)
    if magnitude >= 1e7:
        return f"₹{value / 1e7:.2f}Cr"
    if magnitude >= 1e5:
        return f"₹{value / 1e5:.2f}L"
    if magnitude >= 1e3:
        return f"₹{value / 1e3:.0f}k"
    return f"₹{value:.0f}"
