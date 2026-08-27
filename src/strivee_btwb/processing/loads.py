"""Read the loads an athlete actually lifted out of a BTWB result line.

BTWB records a session's result as three bar-separated fields:

    1295 lbs | 295 lbs, 250 lbs, and 250 lbs | Rx'd ⚡ 80 lbs

Total volume, then the load of every set in the order the rows were written,
then how it was scaled. The middle field is the useful one, and it lines up with
the accessory block's one-row-per-set layout: row N was lifted at load N. That
correspondence is the reason accessory work is written a row per set rather than
"3 sets of 12" — a written round records a single load for all three.
"""

import re
from dataclasses import dataclass

# "295 lbs", "62,5 kg", "100kg" — BTWB writes the unit after the number and uses
# a comma for decimals in French.
_LOAD = re.compile(r"(\d+(?:[.,]\d+)?)\s*(lbs?|kgs?)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Load:
    """One set's load, kept as written so it round-trips into BTWB unchanged."""

    value: float
    unit: str

    def __str__(self) -> str:
        shown = f"{self.value:g}"
        if self.unit.startswith("kg"):
            shown = shown.replace(".", ",")  # BTWB writes kg decimals the French way
        return f"{shown} {self.unit}"


def parse_result_loads(result: str) -> list[Load]:
    """Return the per-set loads from a BTWB result line, in row order.

    Returns ``[]`` for a result with no loads in it — a for-time or max-rep
    session records a duration or a count, and neither says anything about load.
    """
    fields = [f.strip() for f in result.split("|")]
    if len(fields) < 2:
        return []
    # The first field is total volume, a single figure covering the whole
    # session; taking it as a set's load would report one enormous lift.
    return [
        Load(value=float(v.replace(",", ".")), unit=u.lower()) for v, u in _LOAD.findall(fields[1])
    ]


def loads_by_movement(rows: list[str], result: str = "") -> dict[str, list[Load]]:
    """Read movement → loads from the rows of a logged session.

    Each logged row carries its own load ("2 Back Squats | 250 lbs"), so the
    movement and the weight come from the same line and no positional pairing is
    needed. None should be attempted either: the page also carries the session
    date and a per-set level score, and lining loads up by position against those
    put every load on the wrong movement.

    When *result* is given, the count it reports is used as a check. A disagreement
    means rows were missed, so nothing is returned rather than a partial answer
    that would understate what was lifted.
    """
    entries: list[tuple[str, Load]] = []
    for row in rows:
        if "|" not in row:
            continue
        name, _, tail = row.partition("|")
        match = _LOAD.search(tail)
        movement = re.sub(r"^\s*[\d/\-x\s]+", "", name).strip()
        if not match or not movement:
            continue
        entries.append(
            (movement, Load(float(match.group(1).replace(",", ".")), match.group(2).lower()))
        )
    if result and len(parse_result_loads(result)) != len(entries):
        return {}

    # BTWB writes "1 Back Squat" and "2 Back Squats" in the same session. Merge
    # the plural into the singular only when both spellings actually appear —
    # stripping a trailing s unconditionally would turn "Bench Press" into "Pres".
    seen = {name for name, _ in entries}
    paired: dict[str, list[Load]] = {}
    for name, load in entries:
        canonical = name[:-1] if name.endswith("s") and name[:-1] in seen else name
        paired.setdefault(canonical, []).append(load)
    return paired
