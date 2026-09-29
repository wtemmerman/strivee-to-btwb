"""Read a cardio block's main set as fixed-time intervals for BTWB's classic builder.

BTWB's AI generator asks for a distance for every erg interval, or refuses the
block outright, because Strivee prescribes erg work by time and watts. Its
classic "Intervals For Distance" template takes exactly that: a time per interval
and one rest, with the distance logged by the athlete. So the main set is read
deterministically, and a block that does not read cleanly is left to the AI path
rather than guessed at.
"""

import re

from ..core.models import ErgIntervals, ProgrammingBlock

# Most specific first: "Bike Erg" must not read as a run, "Row / Ski" as a row.
_MOVEMENTS = [
    ("Bike Erg", re.compile(r"\bbike\s*erg\b", re.IGNORECASE)),
    ("Ski Erg", re.compile(r"\bski\s*erg\b", re.IGNORECASE)),
    ("Echo Bike", re.compile(r"\becho\s*bike\b", re.IGNORECASE)),
    ("Assault Bike", re.compile(r"\bassault\s*bike\b", re.IGNORECASE)),
    ("Row", re.compile(r"\brow(?:ing)?\b|\brameur\b", re.IGNORECASE)),
    ("Run", re.compile(r"\brun(?:ning)?\b|\bcourse\b|\bfooting\b", re.IGNORECASE)),
]

_SETS_HEADER_RE = re.compile(r"^\s*(\d+)\s*(?:sets?|rounds?)\s*(?:of)?\s*:?\s*$", re.IGNORECASE)
_TRAILING_SETS_RE = re.compile(r"^\s*x\s*(\d+)\s*(?:sets?|rounds?)\s*$", re.IGNORECASE)
_DURATION_RE = re.compile(
    r"^\s*(\d+)\s*(min(?:ute)?s?|'|sec(?:ond)?s?|s)(?![a-z])\s*(.*)$", re.IGNORECASE
)
_IN_SET_REST_RE = re.compile(r"\brest\b|\brepos\b|r[ée]cup", re.IGNORECASE)
_NO_REST_RE = re.compile(r"^\s*-?\s*no\s+rest\b", re.IGNORECASE)
_REST_BETWEEN_RE = re.compile(
    r"^\s*-?\s*rest\s+(\d+)\s*(min(?:ute)?s?|'|sec(?:ond)?s?|s)(?![a-z]).*\bbetween\b",
    re.IGNORECASE,
)
# What follows the time on an erg line is an intensity. Anything else ("Max strict
# HSPU") means this is not an erg set, whatever the block mentions elsewhere.
_INTENSITY_RE = re.compile(
    r"%|\d\s*w\b|watt|ftp|rpe|pace|easy|hard|zone|\bz\d|bpm|rpm|recovery|allure|"
    r"bike|erg|\brow|\brun|\bski",
    re.IGNORECASE,
)


_WARM_UP_RE = re.compile(r"warm[\s-]?up|[ée]chauffement", re.IGNORECASE)
_COOLDOWN_RE = re.compile(r"cool[\s-]?down|retour au calme", re.IGNORECASE)
# A section header standing alone: "Warm-up", "TEST -", "Main Part :".
_HEADER_RE = re.compile(r"^\s*[A-Za-zÀ-ÿ][^\d]*?\s*[-:]?\s*$")
_ANY_REST_RE = re.compile(r"^\s*-?\s*(?:no\s+)?rest\b", re.IGNORECASE)


def _seconds(value: str, unit: str) -> int:
    return int(value) * (1 if unit.lower().startswith("s") else 60)


def _movement(block: ProgrammingBlock) -> str | None:
    """The one erg the block works on, read from its title and workout first.

    The note is only asked when those name none — a run titled "Optional EF" says
    "Run" only there — since a note can mention another erg in passing: the 08-24
    sprints' note opens with "The Rowing Club".
    """
    for text in (f"{block.name}\n{block.content}", block.instruction):
        found = [name for name, pattern in _MOVEMENTS if pattern.search(text)]
        if found:
            return found[0] if len(found) == 1 else None
    return None


def _durations(lines: list[str]) -> list[tuple[int, str]] | None:
    parsed = []
    for line in lines:
        m = _DURATION_RE.match(line)
        label = m.group(3) if m else ""
        if not m or (label and not (_INTENSITY_RE.search(label) or _IN_SET_REST_RE.search(label))):
            return None
        parsed.append((_seconds(m.group(1), m.group(2)), m.group(3)))
    return parsed


def _main_set(lines: list[str]) -> tuple[int, list[str], str | None] | None:
    """(sets, the set's lines, its between-sets rest line) for the one main set."""
    headers = [(i, m) for i, line in enumerate(lines) if (m := _SETS_HEADER_RE.match(line))]
    trailing = [(i, m) for i, line in enumerate(lines) if (m := _TRAILING_SETS_RE.match(line))]
    body: list[str] = []
    if len(headers) == 1 and not trailing:
        start, header = headers[0]
        for line in lines[start + 1 :]:
            if not line.strip():
                continue
            if _NO_REST_RE.match(line) or _REST_BETWEEN_RE.match(line):
                return int(header.group(1)), body, line
            if not _DURATION_RE.match(line):
                break
            body.append(line)
        return int(header.group(1)), body, None
    if len(trailing) == 1 and not headers:
        end, count = trailing[0]
        for line in reversed(lines[:end]):
            if not _DURATION_RE.match(line):
                break
            body.insert(0, line)
        return int(count.group(1)), body, None
    return None


def _between_sets_rest(rest_line: str | None) -> int | None:
    if rest_line and _NO_REST_RE.match(rest_line):
        return 0
    if rest_line and (m := _REST_BETWEEN_RE.match(rest_line)):
        return _seconds(m.group(1), m.group(2))
    return None


def _one_rest(work: int, in_set_rest: int | None, between: int | None) -> int | None:
    """The single rest BTWB can hold for a set of *work* pieces, or None if there is none.

    BTWB takes one rest for every interval, so the set must rest the same after
    each piece of work: either one piece per set, or no rest at all.
    """
    if work == 1:
        if in_set_rest is not None and between not in (None, in_set_rest):
            return None
        return in_set_rest if in_set_rest is not None else (between or 0)
    return None if in_set_rest or between else 0


# "3 x (20 sec Bike erg #HARD PACE - 40 sec # Recovery Pace)": work then recovery,
# repeated.
_REPEATS_RE = re.compile(
    r"^\s*(\d+)\s*x\s*\(\s*(\d+)\s*(min(?:ute)?s?|'|sec(?:ond)?s?|s)(?![a-z])([^)]*?)\s+[-\u2013]\s+"
    r"(\d+)\s*(min(?:ute)?s?|'|sec(?:ond)?s?|s)(?![a-z])([^)]*)\)\s*$",
    re.IGNORECASE,
)
_RECOVERY_RE = re.compile(r"recovery|r[ée]cup|easy|rest|repos", re.IGNORECASE)


def _repeats(lines: list[str]) -> tuple[tuple[int, ...], int] | None:
    """(intervals, rest) for a block that is one "N x (work - recovery)" line.

    The recovery becomes BTWB's rest between intervals; two efforts with no
    recovery between them alternate as intervals instead.
    """
    lines = [line for line in lines if line.strip()]
    if len(lines) != 1 or not (m := _REPEATS_RE.match(lines[0])):
        return None
    count = int(m.group(1))
    work, second = _seconds(m.group(2), m.group(3)), _seconds(m.group(5), m.group(6))
    if _RECOVERY_RE.search(m.group(7)):
        return (work,) * count, second
    return (work, second) * count, 0


def _steady_effort(lines: list[str]) -> int | None:
    """Seconds of the one timed effort a block holds besides its warm-up, or None.

    "5min Warm-up … / 40min Steady State #55-60% FTP 20 / Total - 45min" and
    "Warm-up / 6min … / - Rest 1Min and GO - / TEST - / 20min Max wattage Bike erg"
    are one 40- and one 20-minute effort. A range ("45-60min") or a distance
    ("1200m Run") is not a time BTWB can hold, so it leaves the block alone.
    """
    efforts: list[int] = []
    in_warm_up = False
    for line in (line for line in lines if line.strip()):
        if _HEADER_RE.match(line):
            in_warm_up = bool(_WARM_UP_RE.search(line))
            continue
        if _ANY_REST_RE.match(line):
            in_warm_up = False
            continue
        if in_warm_up or _WARM_UP_RE.search(line) or _COOLDOWN_RE.search(line):
            continue
        if not line.lstrip()[:1].isdigit():
            continue  # coaching or a header naming the session ("Total - 45min")
        timed = _durations([line])
        if timed is None:
            return None  # a distance, a range, anything not a plain time
        efforts.append(timed[0][0])
    return efforts[0] if len(efforts) == 1 else None


def erg_intervals(block: ProgrammingBlock) -> ErgIntervals | None:
    """The block's main set as fixed-time intervals, or None if it does not read as one."""
    movement = _movement(block)
    lines = block.content.splitlines()
    found = _main_set(lines) if movement else None
    if movement and found is None and (repeats := _repeats(lines)) is not None:
        return ErgIntervals(movement=movement, intervals=repeats[0], rest_seconds=repeats[1])
    if movement and found is None and (steady := _steady_effort(lines)) is not None:
        return ErgIntervals(movement=movement, intervals=(steady,), rest_seconds=0)
    durations = _durations(found[1]) if found else None
    if not movement or not found or not durations:
        return None

    in_set_rest = durations.pop()[0] if _IN_SET_REST_RE.search(durations[-1][1]) else None
    if not durations or any(_IN_SET_REST_RE.search(label) for _, label in durations):
        return None
    rest = _one_rest(len(durations), in_set_rest, _between_sets_rest(found[2]))
    if rest is None:
        return None
    return ErgIntervals(
        movement=movement,
        intervals=tuple(secs for secs, _ in durations) * found[0],
        rest_seconds=rest,
    )


def clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def describe(plan: ErgIntervals) -> str:
    """What preview shows for an erg block, in place of the workout text BTWB won't see."""
    times = plan.intervals
    if len(times) == 1:
        return f"{plan.movement} - Intervals For Distance\n1 x {clock(times[0])}"
    if len(set(times)) == 1:
        work = f"{len(times)} x {clock(times[0])}"
    else:
        work = f"{len(times)} intervals: " + ", ".join(clock(t) for t in times)
    rest = f"rest {clock(plan.rest_seconds)}" if plan.rest_seconds else "no rest"
    return f"{plan.movement} - Intervals For Distance\n{work}, {rest}"
