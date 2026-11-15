"""Apply a level published only as changes to the RX workout.

Strivee sometimes publishes a scaled level as nothing but what differs from RX:
a scale chain from the RX movement to its easier versions ("Scale - 6 Bar muscle-up
-> 3 Bar Muscle-up -> 8 Burpees bar muscle-up"), or the scaled lines joined on one
line ("400/300m Row - 20 Thruster #15/10kg - 10 Pull-up (Banded or Jumping)").
Posted as is, BTWB got a one-line workout with no structure, so the 10-19, 10-26,
11-04, 11-11 and 11-17 levels were rewritten by hand. The RX workout with those lines
swapped in is what that level actually does.
"""

import re

# "Scale - 6 Bar muscle-up -> 3 Bar Muscle-up -> 8 Burpees bar muscle-up"
_SCALE_RE = re.compile(r"^\s*scale\s*[-:]\s*(.+?->.+?)\s*$", re.IGNORECASE)
# The pieces of "Same as RX - 25 Thruster #15/10kg - 15 Pull-up"; "Pull-up" has no spaces.
_PIECE_SEPARATOR_RE = re.compile(r"\s+[-\u2013]\s+")
_SAME_AS_RX_RE = re.compile(r"^\s*same\s+as\s+rx\s*$", re.IGNORECASE)
# "10-6 Bar Muscle-up" (men / women), "500/400m Row", "20/16 Calories Row"
_QUANTITY_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?(?:\s*[-/]\s*\d+(?:[.,]\d+)?)*\s*(?:k?m|cal(?:orie)?s?|reps?)?(?![a-z])\s*",
    re.IGNORECASE,
)
# What follows the movement — "#20/15kg", "@70%", "(Banded or Jumping)" — differs by level.
_LOAD_OR_CUE_RE = re.compile(r"\s*[#@(].*$")
# "RX - Target : Sub 4:00" belongs to the level being replaced.
_RX_HEADER_RE = re.compile(r"^\s*RX\b")


def _parts(text: str) -> tuple[str, str, str]:
    """(quantity, movement as written, load or cue) of a line; any of them may be empty."""
    m = _QUANTITY_RE.match(text)
    quantity = m.group(0) if m and m.group(0).strip() else ""
    rest = text[len(quantity) :]
    load = _LOAD_OR_CUE_RE.search(rest)
    name = rest[: load.start()] if load else rest
    return quantity, re.sub(r"\s+", " ", name).strip(), load.group(0).strip() if load else ""


def _name(text: str) -> str:
    return _parts(text)[1].lower()


def _movement(text: str) -> str | None:
    """The movement a counted line names, with its quantity, load and cues dropped.

    "3 sets of :" counts a header, not a movement.
    """
    quantity, name, _load = _parts(text)
    if not quantity or not name or name.endswith(":"):
        return None
    return name.lower()


def _is_change(piece: str) -> bool:
    """A counted movement ("10 Pull-up"), or a load or cue for one ("KB #20/12kg")."""
    quantity, name, load = _parts(piece)
    return bool(name) and bool(quantity or load) and not name.endswith(":")


def _swaps(level: str) -> tuple[list[tuple[str, str]], bool] | None:
    """(the RX text each change replaces, its replacement) for each change *level* makes,
    and whether its pieces may stand in for the RX lines by position.

    None when *level* is anything but changes: every line a scale chain, or one
    line of two or more pieces, optionally opening with "Same as RX".
    """
    lines = [line for line in level.splitlines() if line.strip()]
    if not lines:
        return None
    chains = [m.group(1).split("->") for line in lines if (m := _SCALE_RE.match(line))]
    if len(chains) == len(lines):
        pairs = [(steps[0].strip(), steps[1].strip()) for steps in chains]
        positional = False
    elif len(lines) == 1:
        pieces = _PIECE_SEPARATOR_RE.split(lines[0].strip())
        same_as_rx = bool(_SAME_AS_RX_RE.match(pieces[0]))
        pieces = pieces[1:] if same_as_rx else pieces
        if not pieces or (len(pieces) < 2 and not same_as_rx):
            return None
        pairs = [(piece, piece) for piece in pieces]
        # A level that lists every movement is the whole workout in order; one that
        # opens "Same as RX" names only what it changes.
        positional = not same_as_rx
    else:
        return None
    if not all(_is_change(old) and _is_change(new) for old, new in pairs):
        return None
    return pairs, positional


def _line_for(lines: list[str], counted: list[int], old: str) -> int | None:
    """The one counted RX line *old* names: same movement, or one it is the start of.

    "10/7 Cal Bike" names "12/9 Calories Bike erg"; "KB #20/12kg" names
    "10 KB Swing #24/16kg". Two candidates name none.
    """
    name = _name(old)
    exact = [i for i in counted if _movement(lines[i]) == name]
    if exact:
        return exact[0] if len(exact) == 1 else None
    starts = [i for i in counted if (_movement(lines[i]) or "").startswith(f"{name} ")]
    return starts[0] if len(starts) == 1 else None


def _rewritten(rx_line: str, new: str) -> str:
    """*rx_line* as *new* changes it.

    A load alone ("KB #20/12kg") or a shortened name ("10/7 Cal Bike") keeps the RX
    line's count or full movement name; a counted line naming its own movement
    replaces the RX line outright.
    """
    rx_quantity, rx_name, _rx_load = _parts(rx_line)
    quantity, name, load = _parts(new)
    if quantity and not rx_name.lower().startswith(f"{name.lower()} "):
        return new
    return " ".join(part for part in (f"{quantity or rx_quantity}{rx_name}", load) if part)


def apply_to_rx(rx: str, level: str) -> str | None:
    """*rx* with *level*'s changes swapped in, or None when *level* is not only changes.

    Each change must name the movement of exactly one RX line, and no two the same
    line. A level listing one piece per counted RX line may also swap a movement
    in by position ("8 Hanging knee raise" for "8 Toes to bar"), as long as every
    piece that does name its line sits at that line's position. Anything else is
    left as published.
    """
    parsed = _swaps(level)
    if parsed is None:
        return None
    pairs, positional = parsed
    lines = rx.splitlines()
    counted = [i for i, line in enumerate(lines) if _movement(line)]
    by_position = positional and len(pairs) == len(counted)
    replaced: dict[int, str] = {}
    for n, (old, new) in enumerate(pairs):
        at = _line_for(lines, counted, old)
        if at is None:
            if not by_position or not _movement(new):
                return None
            at, line = counted[n], new
        elif by_position and at != counted[n]:
            return None
        else:
            line = _rewritten(lines[at], new)
        if at in replaced:
            return None
        replaced[at] = line
    for at, line in replaced.items():
        lines[at] = line
    return "\n".join(line for line in lines if not _RX_HEADER_RE.match(line))
