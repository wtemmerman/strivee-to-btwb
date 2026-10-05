"""Read a single-movement set scheme for BTWB's classic "Sets" template.

BTWB's AI generator refuses some plain set schemes outright — "2 sets of : Max rep
strict HSPU with Abmat" came back with no preview however often it was asked —
while its classic Sets template takes them directly. Only a block that is nothing
but the scheme is read; anything more stays on the AI path.
"""

import re

from ..core.models import AlternatingEmom, ClassicSets, ProgrammingBlock
from .plus_split import unsplit_name
from .timing import clock, seconds

_SETS_HEADER_RE = re.compile(r"^\s*(\d+)\s*sets?\s*(?:of)?\s*:?\s*$", re.IGNORECASE)
_MAX_REP_RE = re.compile(r"^\s*max\s*reps?\s+(.+?)\s*$", re.IGNORECASE)
# A rest range ("Rest 2-3 min") posts its lower bound: BTWB holds one rest, and the
# note opens with the range as written.
_REST_RE = re.compile(
    r"^\s*-?\s*rest\s+(\d+)(?:\s*-\s*\d+)?\s*(min(?:ute)?s?|'|sec(?:ond)?s?|s)(?![a-z])"
    r"(?:\s+between\s+sets?)?\s*-?\s*$",
    re.IGNORECASE,
)
_REPS_LINE_RE = re.compile(r"^\s*(\d+)\s*reps?\s+(.+?)(?:\s+tempo\s+(\w+))?\s*$", re.IGNORECASE)
_TARGET_1RM_RE = re.compile(
    r"^\s*target\s+weight\s*:\s*[#@]?\s*(\d+)\s*%\s*(?:of\s+)?1\s*RM\b", re.IGNORECASE
)
_WINDOW_RE = re.compile(r"^\s*in\s+an?\s+\d+\s*min(?:ute)?s?\s+window\s*:?\s*$", re.IGNORECASE)
_RM_LINE_RE = re.compile(r"^\s*(\d+)\s*RM\s+(.+?)\s*$", re.IGNORECASE)
_BUILD_HEAVY_RE = re.compile(
    r"^\s*build\s+(?:up\s+to\s+)?(?:a|an)?\s*(?:new\s+)?heavy\s+(single|double|triple)\s*-?\s*(.+?)\s*$",
    re.IGNORECASE,
)
_HEAVY_REPS = {"single": 1, "double": 2, "triple": 3}
_TOP_SET_RE = re.compile(r"^\s*(?:[A-Z]\.\s*)?top\s*set\b\s*[-:]?\s*$", re.IGNORECASE)
_BACK_OFF_RE = re.compile(r"^\s*(?:[A-Z]\.\s*)?back\s*off\b\s*[-:]?\s*$", re.IGNORECASE)
# "1x4 RPE 9 (Target - 284.5 lb-303 lb)", "2x4 #90% of your today Top set"
_SETS_BY_REPS_RE = re.compile(r"^\s*(\d+)\s*x\s*(\d+)\b", re.IGNORECASE)
# "Build a set of 3 Reps RPE 6", "Build a set of 1Reps RPE 7"
_BUILD_A_SET_RE = re.compile(r"^\s*build\s+a\s+set\s+of\s+(\d+)\s*reps?\b", re.IGNORECASE)
# "2 Sets of 4 Reps @90% of your today 3 reps"
_SETS_OF_REPS_RE = re.compile(r"^\s*(\d+)\s*sets?\s+of\s+(\d+)\s*reps?\b", re.IGNORECASE)
# "- Rest 2min between sets -", "- Rest 1min30 between sets -"; a range ("1min30 -
# 2min") keeps its first bound, the note carries the rest.
_REST_LEAD_RE = re.compile(
    r"^\s*-?\s*rest\s+(\d+)\s*(min(?:ute)?s?|'|sec(?:ond)?s?|s)(?![a-z])\s*(\d+)?", re.IGNORECASE
)
_TEMPO_RE = re.compile(r"\btempo\b", re.IGNORECASE)
_EMOM_RE = re.compile(
    r"^\s*EMOM\s*x?\s*(\d+)\s*(?:min(?:ute)?s?|sets?)?\s*:?\s*(.*)$", re.IGNORECASE
)
# "Every 75 sec x 6 sets of :", "Every 1min30 x 4 sets of:", "Every 90 sec x 5 sets"
_EVERY_RE = re.compile(
    r"^\s*every\s+(\d+)\s*(min(?:ute)?s?|'|sec(?:ond)?s?|s)(?![a-z])\s*(\d+)?\s*x\s*(\d+)\s*"
    r"(?:sets?|rounds?)?\s*(?:of)?\s*:?\s*(.*)$",
    re.IGNORECASE,
)
# A bracket naming another lift — "(Clean + back rack + jerk)", "(Clean and Jerk
# Start)" — says the movement is part of a complex.
_BRACKETED_LIFT_RE = re.compile(r"\([^)]*\b(?:clean|jerk|snatch)\b[^)]*\)", re.IGNORECASE)


def _is_complex(movement_text: str) -> bool:
    return "+" in movement_text or bool(_BRACKETED_LIFT_RE.search(movement_text))


_RM_LOAD_RE = re.compile(r"\d+\s*%.*?(?<![a-z])\d*\s*RM\b", re.IGNORECASE)
_EMOM_STRUCTURE_RE = re.compile(r"^\s*(?:x\s*\d+|sets?\s*\d+|-?\s*rest\b)", re.IGNORECASE)
_EMOM_REPS_RE = re.compile(r"^\s*(\d+)\s*(?:reps?\s+)?([A-Za-z].*?)\s*$", re.IGNORECASE)
# "2-pause Squat clean", "Squat clean with pause": BTWB names it "Pause Squat Clean".
_COUNTED_PAUSE_RE = re.compile(r"^\s*\d+\s*-\s*pause\s+", re.IGNORECASE)
_WITH_PAUSE_RE = re.compile(r"\s+with\s+(?:a\s+)?pause\b.*$", re.IGNORECASE)
# A trailing cue — "#Bellow and above the knee", "@eyes level", "(5 sec floor to
# hip)" — is coaching.
_CUE_RE = re.compile(r"\s+[#@].*$|\s*\([^)]*\)")
# BTWB's names where Strivee's differ.
_ALIASES = {"barbell seal row": "Seal Row"}
# The aid a movement is scaled with is coaching, not the movement: BTWB has
# "Strict Handstand Push-up", the note keeps "with Abmat".
_AID_RE = re.compile(r"\s+(?:with|avec)\s+.*$", re.IGNORECASE)
# Coaching qualifiers, not part of any BTWB movement name.
_QUALIFIER_RE = re.compile(
    r"\b(?:unbroken|ub|touch\s+and\s+go)\b|@?\s*\brpe\s*[\d.,-]+", re.IGNORECASE
)
_TITLE_MOVEMENT_RE = re.compile(r"^EMF\s+\w+\s*[:\-]\s*(.+)$", re.IGNORECASE)
# "(OPTION)", "- OPTION" say the block is optional, not which movement it is.
_TITLE_NOISE_RE = re.compile(r"\(.*?\)|[-\s]+option\b", re.IGNORECASE)
_PHRASES = [
    (re.compile(r"\bchest[\s-]+to[\s-]+bar\b", re.IGNORECASE), "Chest-to-bar"),
    (re.compile(r"\btoes[\s-]+to[\s-]+bar\b", re.IGNORECASE), "Toes-to-bar"),
    (re.compile(r"\s+and\s+jerk\b", re.IGNORECASE), " & Jerk"),
]
_ABBREVIATIONS = {
    "hspu": "Handstand Push-up",
    "c2b": "Chest-to-bar Pull-up",
    "t2b": "Toes-to-bar",
    "ttb": "Toes-to-bar",
    "du": "Double Under",
    "dus": "Double Unders",
    "rmu": "Ring Muscle-up",
    "bmu": "Bar Muscle-up",
}


def btwb_movement_name(text: str) -> str:
    """The BTWB name a Strivee movement is written as: aids dropped, abbreviations expanded.

    Posting looks the result up by exact name and skips the block, reported for
    adding by hand, when BTWB has no such movement: a wrong guess never posts the
    wrong movement.
    """
    text = _CUE_RE.sub("", text)
    # Unlike an aid, a pause names a different BTWB movement.
    if _WITH_PAUSE_RE.search(text):
        text = "Pause " + _WITH_PAUSE_RE.sub("", text)
    text = _QUALIFIER_RE.sub("", _AID_RE.sub("", text))
    text = _COUNTED_PAUSE_RE.sub("Pause ", text)
    for pattern, name in _PHRASES:
        text = pattern.sub(name, text)
    words = [_ABBREVIATIONS.get(w.lower(), w) for w in text.split()]
    # Shouted words ("RING") are written as BTWB spells names; expansions stay as given.
    words = [w.capitalize() if w.isupper() and len(w) > 1 else w for w in words]
    name = " ".join(w[:1].upper() + w[1:] for w in " ".join(words).split())
    return _ALIASES.get(name.lower(), name)


def _title_movement(block: ProgrammingBlock) -> str:
    """The movement a block's title names: "EMF 60 : Bench Press (1/2)" → "Bench Press"."""
    name = unsplit_name(block.name)
    m = _TITLE_MOVEMENT_RE.match(name)
    return _TITLE_NOISE_RE.sub("", m.group(1) if m else name).strip()


def _squashed(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _agrees_with_title(movement_text: str, block: ProgrammingBlock) -> bool:
    """Whether the movement read from a line is the one the block's title names.

    "Clean" and "2-pause Squat clean" agree; "Back Squat" and "3RM en 4 semaines"
    — a programme header read as a rep max — do not, and the block is left alone.
    """
    read, title = _squashed(btwb_movement_name(movement_text)), _squashed(_title_movement(block))
    return bool(read and title) and (read in title or title in read)


def _max_rep_sets(lines: list[str]) -> ClassicSets | None:
    """ "N sets of : / Max rep <movement> / - Rest X -" and nothing more."""
    if len(lines) not in (2, 3):
        return None
    header, movement = _SETS_HEADER_RE.match(lines[0]), _MAX_REP_RE.match(lines[1])
    rest = _REST_RE.match(lines[2]) if len(lines) == 3 else None
    if not header or not movement or (len(lines) == 3 and not rest):
        return None
    return ClassicSets(
        movement=btwb_movement_name(movement.group(1)),
        reps=(None,) * int(header.group(1)),
        rest_seconds=seconds(rest.group(1), rest.group(2)) if rest else None,
    )


def _weighted_sets(lines: list[str]) -> ClassicSets | None:
    """ "N sets of : / R Reps <movement> [Tempo 31X1] / - Rest X - / Target weight : #P% 1RM"."""
    if len(lines) not in (3, 4):
        return None
    header, reps, rest = (
        _SETS_HEADER_RE.match(lines[0]),
        _REPS_LINE_RE.match(lines[1]),
        _REST_RE.match(lines[2]),
    )
    target = _TARGET_1RM_RE.match(lines[3]) if len(lines) == 4 else None
    if not header or not reps or not rest or (len(lines) == 4 and not target):
        return None
    # A load on the reps line ("@75-80% of your 1RM") is a range or a reference BTWB's
    # one % per set cannot hold; the AI path keeps it in the workout.
    if "%" in reps.group(2):
        return None
    movement = btwb_movement_name(reps.group(2))
    if not movement:
        return None
    return ClassicSets(
        movement=f"Tempo {movement}" if reps.group(3) else movement,
        reps=(int(reps.group(1)),) * int(header.group(1)),
        rest_seconds=seconds(rest.group(1), rest.group(2)),
        percent_1rm=int(target.group(1)) if target else None,
    )


def _emom_header(line: str) -> tuple[int, int, str] | None:
    """(seconds per set, sets, what follows on the line) for an EMOM or "Every" header."""
    if m := _EMOM_RE.match(line):
        return 60, int(m.group(1)), m.group(2)
    if m := _EVERY_RE.match(line):
        every = seconds(m.group(1), m.group(2)) + int(m.group(3) or 0)
        return every, int(m.group(4)), m.group(5)
    return None


def _emom(lines: list[str]) -> ClassicSets | None:
    """One movement, one rep count, a set every N seconds: BTWB's EMOM.

    "EMOMx12 : / 6 reps Butterfly Chest to bar pull-up", "Every 75 sec x 6 sets of : /
    1 Slow Pull Squat Snatch (5 sec floor to hip) / #70 to 80% …", "EMOMx6: 1
    Weighted Dip". Loads, cues and coaching around the movement line go to the
    note; any other line opening with a number — a second movement, a "min 1 -"
    scheme read as such — leaves the block alone.
    """
    headers = [(i, h) for i, line in enumerate(lines) if (h := _emom_header(line))]
    if len(headers) != 1:
        return None
    at, (every, sets, inline) = headers[0]
    if any(line.lstrip()[:1].isdigit() for line in lines[:at]):
        return None
    rest_of_block = ([inline] if inline.strip() else []) + lines[at + 1 :]
    if not rest_of_block or not (movement := _EMOM_REPS_RE.match(rest_of_block[0])):
        return None
    # "+" in the movement is a complex ("Overhead squat from Ground (Clean + back
    # rack + jerk)"); "x4 sets", "Set 1 -" or a rest after it is more structure than
    # one EMOM — the 06-08 clean and jerk was four rounds of EMOMx3.
    # A load of a % of some RM ("#86% of your 1RM", "#85% of your 5RM from week 1")
    # has no place in BTWB's EMOM; the AI path keeps it in the workout.
    if any(_RM_LOAD_RE.search(line) for line in lines):
        return None
    if _is_complex(movement.group(2)) or any(
        line.lstrip()[:1].isdigit() or _EMOM_STRUCTURE_RE.match(line) for line in rest_of_block[1:]
    ):
        return None
    return ClassicSets(
        movement=btwb_movement_name(movement.group(2)),
        reps=(int(movement.group(1)),) * sets,
        rest_seconds=None,
        emom_seconds=every,
    )


def _set_reps(line: str, pattern: re.Pattern[str]) -> list[int] | None:
    m = pattern.match(line)
    return [int(m.group(2))] * int(m.group(1)) if m else None


def _top_set_back_off(lines: list[str], block: ProgrammingBlock) -> ClassicSets | None:
    """A top set then back-off sets, as BTWB Sets at heaviest weight.

    The back-off load is a % of the day's top set, which BTWB cannot express, and
    the RPE has no field; both stay in the note. What is left is the rep scheme,
    which BTWB's AI got wrong: a 1x4 top set and one 1x4 back-off stored as 4x4.
    Lines above the top set are headers — a "Tempo" among them names the movement.
    """
    tops = [i for i, line in enumerate(lines) if _TOP_SET_RE.match(line)]
    backs = [i for i, line in enumerate(lines) if _BACK_OFF_RE.match(line)]
    if len(tops) != 1 or len(backs) != 1 or backs[0] != tops[0] + 2:
        return None
    top, back = tops[0], backs[0]
    top_reps = _set_reps(lines[top + 1], _SETS_BY_REPS_RE)
    if top_reps is None and (m := _BUILD_A_SET_RE.match(lines[top + 1])):
        top_reps = [int(m.group(1))]
    backoff: list[int] = []
    rest = None
    for line in lines[back + 1 :]:
        found = _set_reps(line, _SETS_BY_REPS_RE) or _set_reps(line, _SETS_OF_REPS_RE)
        if found is None:
            if m := _REST_LEAD_RE.match(line):
                rest = seconds(m.group(1), m.group(2)) + int(m.group(3) or 0)
            break
        backoff.extend(found)
    if not top_reps or not backoff:
        return None
    movement = btwb_movement_name(_title_movement(block))
    if any(_TEMPO_RE.search(line) for line in lines[:top]) and not _TEMPO_RE.search(movement):
        movement = f"Tempo {movement}"
    return ClassicSets(movement=movement, reps=tuple(top_reps + backoff), rest_seconds=rest)


def _rep_max(lines: list[str]) -> ClassicSets | None:
    """ "[In a N min window :] / 10RM <movement>" or "Build a heavy double - <movement>".

    Whatever follows the rep-max line is coaching, and the note already carries it.
    """
    start = 1 if lines and _WINDOW_RE.match(lines[0]) else 0
    if len(lines) <= start:
        return None
    line = lines[start]
    if m := _RM_LINE_RE.match(line):
        reps, text = int(m.group(1)), m.group(2)
    elif m := _BUILD_HEAVY_RE.match(line):
        reps, text = _HEAVY_REPS[m.group(1).lower()], m.group(2)
    else:
        return None
    return ClassicSets(
        movement=btwb_movement_name(text), reps=(reps,), rest_seconds=None, rep_max=True
    )


def classic_sets(block: ProgrammingBlock) -> ClassicSets | None:
    """The block as one of the single-movement schemes BTWB's classic builder takes.

    None for anything else — a block only partly read would post a different
    workout, while the AI path at worst posts it badly. A reps line that names no
    movement ("6 Reps RPE 7") takes the title's.
    """
    lines = [line for line in block.content.splitlines() if line.strip()]
    if len(lines) > 1 and (m := _REPS_LINE_RE.match(lines[1])):
        if not btwb_movement_name(m.group(2)):
            lines[1] = f"{m.group(1)} Reps {_title_movement(block)}"
    plan = (
        _max_rep_sets(lines)
        or _weighted_sets(lines)
        or _emom(lines)
        or _top_set_back_off(lines, block)
        or _rep_max(lines)
    )
    if plan is None or not _agrees_with_title(plan.movement, block):
        return None
    return plan


# "min 1 - 1 Squat Clean", "Min 2 : 12 Box Jump Over", "Odd minutes (1-3-5-7): 8 Bar
# Muscle-up", "Even: 6 Chest to bar pull-up".
_TURN_RE = re.compile(
    r"^\s*(?:min(?:ute)?\s*(?P<minute>\d+)|(?P<parity>odd|even)(?:\s+min(?:ute)?s?)?"
    r"(?:\s*\([^)]*\))?)\s*[-:]\s*(?P<work>.+?)\s*$",
    re.IGNORECASE,
)
# "#70% of your 1RM", "@75% 1RM": the load of every movement in the EMOM.
_PERCENT_LINE_RE = re.compile(
    r"^\s*[#@]?\s*(\d+)\s*%\s*(?:of\s+(?:your\s+)?)?1\s*RM\s*$", re.IGNORECASE
)
# Work measured in something other than reps — "45 sec Ski Erg", "15 Cal Ski Erg",
# "200m Run" — is not a rep count BTWB's reps field can hold.
_NOT_REPS_RE = re.compile(r"^(?:sec(?:ond)?s?|s|cal(?:orie)?s?|m|meters?|min(?:ute)?s?)\b", re.I)


def _turn(work: str) -> tuple[int, str] | None:
    """(reps, BTWB movement name) for one interval's work, or None if it is not that."""
    m = _EMOM_REPS_RE.match(work)
    if not m or _NOT_REPS_RE.match(m.group(2)) or _is_complex(m.group(2)) or "%" in work:
        return None
    name = btwb_movement_name(m.group(2))
    return (int(m.group(1)), name) if name else None


def _turns(lines: list[str]) -> list[tuple[int, str]] | None:
    """(reps, movement) for each interval of the cycle *lines* open with, in order.

    The cycle is "min 1 … min N" numbered from 1, or exactly "Odd … / Even …".
    """
    turns: list[re.Match[str]] = []
    for line in lines:
        if not (m := _TURN_RE.match(line)):
            break
        turns.append(m)
    minutes = [m.group("minute") for m in turns]
    parities = [(m.group("parity") or "").lower() for m in turns]
    if len(turns) < 2 or (
        minutes != [str(n) for n in range(1, len(turns) + 1)] and parities != ["odd", "even"]
    ):
        return None
    read = [_turn(m.group("work")) for m in turns]
    return None if any(r is None for r in read) else [r for r in read if r is not None]


def alternating_emom(block: ProgrammingBlock) -> AlternatingEmom | None:
    """Movements taking turns minute by minute: BTWB's alternating EMOM.

    "EMOMx8 / min 1 - 1 Squat Clean with pause @knee level / min 2 - 1 Squat clean /
    #70% of your 1RM", or the same as "Odd: … / Even: …". Every turn must be a whole
    rep count of one movement and the minutes must divide into full cycles; a rest
    minute, a time or calorie target, a rep range or a per-movement % load leaves the
    block on the AI path. Cues and coaching around it go to the note, as for _emom.
    """
    lines = [line for line in block.content.splitlines() if line.strip()]
    headers = [i for i, line in enumerate(lines) if _EMOM_RE.match(line)]
    header = _EMOM_RE.match(lines[headers[0]]) if len(headers) == 1 else None
    # "EMOMx6-8" has no one length to divide into cycles.
    if header is None or header.group(2).strip():
        return None
    at = headers[0]
    turns = _turns(lines[at + 1 :])
    total = int(header.group(1))
    if (
        turns is None
        or total % len(turns)
        or any(line.lstrip()[:1].isdigit() for line in lines[:at])
    ):
        return None

    after = lines[at + 1 + len(turns) :]
    percent = None
    if after and (m := _PERCENT_LINE_RE.match(after[0])):
        percent, after = int(m.group(1)), after[1:]
    # Any other % — "#65 to 75% 120.5 lb" — is a load BTWB's one % per movement cannot hold.
    if any(
        line.lstrip()[:1].isdigit() or _EMOM_STRUCTURE_RE.match(line) or "%" in line
        for line in after
    ):
        return None
    return AlternatingEmom(
        movements=tuple(name for _reps, name in turns),
        reps=tuple(reps for reps, _name in turns),
        every_seconds=60,
        sets_per_movement=total // len(turns),
        percent_1rm=percent,
    )


def describe_alternating(plan: AlternatingEmom) -> str:
    """What preview shows for an alternating EMOM block."""
    total = plan.every_seconds * plan.sets_per_movement * len(plan.movements)
    load = f" @ {plan.percent_1rm}% 1RM" if plan.percent_1rm is not None else ""
    turns = "\n".join(f"{reps} {name}{load}" for reps, name in zip(plan.reps, plan.movements))
    return (
        f"Alternating EMOM\nevery {clock(plan.every_seconds)} for {clock(total)}, "
        f"{plan.sets_per_movement} sets per movement:\n{turns}"
    )


def describe_sets(plan: ClassicSets) -> str:
    """What preview shows for a classic Sets block."""
    if plan.rep_max:
        return f"{plan.movement} - X Rep Max\n{plan.reps[0]} rep max"
    if plan.emom_seconds is not None:
        every, total = plan.emom_seconds, plan.emom_seconds * len(plan.reps)
        rep_word = "rep" if plan.reps[0] == 1 else "reps"
        return (
            f"{plan.movement} - EMOM\n{plan.reps[0]} {rep_word} every {clock(every)} "
            f"for {clock(total)}"
        )
    reps = ", ".join("max" if r is None else str(r) for r in plan.reps)
    load = f" @ {plan.percent_1rm}% 1RM" if plan.percent_1rm is not None else ""
    seconds = plan.rest_seconds
    rest = "rest as needed" if seconds is None else f"rest {seconds // 60}:{seconds % 60:02d}"
    sets = f"{len(plan.reps)} set{'s' if len(plan.reps) > 1 else ''}"
    return f"{plan.movement} - Sets\n{sets}: {reps} reps{load}, {rest}"
