"""Check what BTWB stored against what was sent to it.

BTWB parses a posted workout with an AI that resolves a movement it does not
recognise to an arbitrary other one rather than failing. Measured over two real
weeks, about one block in six came back altered: "3 Weighted Strict
Chest-to-Ring Pull-up" was stored as "Burpee Box Jump Over + 12/9 Muscle Ups",
"2 Clean and jerk" as "Snatch Balance+1". Common barbell and gymnastics work
survives; ring and skill work is where it invents.

Nothing here can stop that. What it can do is make it visible: every word of a
stored movement should appear in the text that produced it, so a line that
shares almost nothing with its source is reported. The check is deliberately
one-directional — it catches substitution, not omission, because a dropped
movement leaves nothing behind to compare.

It is also deliberately narrow: only lines BTWB renders with a leading rep count
are checked. A movement stored without one ("Echo Bike Calorie", "Power Snatch :
5 Rep Max") goes unexamined. That is the cost of a report worth reading — the
alternative flagged every form label on the page and buried the two real
substitutions in thirty-five false ones.
"""

import re
from dataclasses import dataclass

_WORD = re.compile(r"[a-z0-9]+")

# BTWB renders a prescription as "<count> <movement>". Only those lines are
# checked. The event page also carries form labels ("Schéma de répétitions",
# "Poids par série") and scoring headers, and no rule that tried to enumerate
# those kept up with BTWB's two different event renderings — where a rep count
# separates prescription from chrome cleanly in both.
_PRESCRIPTION_LINE = re.compile(r"^\s*\d")

# Wording the source uses that BTWB legitimately expands, so the expansion is not
# mistaken for a substitution. Applied to the source side only.
_ABBREVIATIONS: dict[str, tuple[str, ...]] = {
    "hspu": ("handstand", "push", "up"),
    "ctb": ("chest", "to", "bar"),
    "c2b": ("chest", "to", "bar"),
    "t2b": ("toes", "to", "bar"),
    "rmu": ("ring", "muscle", "up"),
    "bmu": ("bar", "muscle", "up"),
    "ohs": ("overhead", "squat"),
    "db": ("dumbbell",),
    "kb": ("kettlebell",),
    "sdhp": ("sumo", "deadlift", "high", "pull"),
}

# Units, rep-scheme noise and scoring words carry no movement identity, so they
# neither ground a line nor count against it.
_IGNORED = frozenset(
    """
    rep reps rounds round set sets for time quality cal cals calorie calories
    m km cm mi ft in sec secs second seconds min mins minute minutes hr
    lb lbs kg kgs bw max min amrap amreps amrep emom rft fq tt more
    x of and the a to at on
    rest cap complete possible many pick load
    pour le la les de du temps limite avec sur
    """.split()
)

GROUNDED_FRACTION = 0.75
"""How much of a stored line must trace back to the source to be believed.

Not all of it, because BTWB rewords legitimately: "Strict HSPU" becomes "Strict
Handstand Push-ups" and "Snatch" becomes "Snatches". Every such rewording
measured on real weeks traced back completely once abbreviations were expanded,
while "Clean and jerk" stored as "Snatch Balance+1" traced back exactly half —
so a half threshold let that through. Three quarters allows one added word in
four and still catches it.
"""


@dataclass(frozen=True)
class Mismatch:
    """One stored line that does not trace back to the text it came from.

    Structure and scoring wording is stripped before comparing, in both languages
    BTWB renders: "Pour le temps", "Complete as many rounds as possible", "Rest 1
    min" and "pick load" say nothing about which movement was stored.
    """

    title: str
    stored: str
    grounded: float

    def __str__(self) -> str:
        return (
            f"{self.title}: BTWB stored {self.stored!r} "
            f"({self.grounded:.0%} of it is in the source)"
        )


def _canonical(word: str) -> str:
    """Drop a trailing plural s so both sides are compared in the same form.

    Applied to source and stored alike, so it only has to be consistent, not
    linguistically right: "Pull-ups" and "Pull-up" must agree, and "ups" is too
    short for the prefix rule below to reach.
    """
    return word[:-1] if len(word) >= 3 and word.endswith("s") else word


def _words(text: str) -> list[str]:
    return [
        _canonical(w) for w in _WORD.findall(text.lower()) if w not in _IGNORED and not w.isdigit()
    ]


def source_vocabulary(source: str) -> set[str]:
    """Every word the source could legitimately have produced, abbreviations expanded."""
    vocabulary = set()
    for word in _words(source):
        vocabulary.add(word)
        vocabulary.update(_ABBREVIATIONS.get(word, ()))
    return vocabulary


def _traces_back(word: str, vocabulary: set[str]) -> bool:
    """Is *word* in the source, allowing for plurals in either direction?

    Prefix matching rather than stemming: "Snatches" against "Snatch" and
    "Raises" against "Raise" pluralise differently, and every stemmer simple
    enough to inline gets one of them wrong.
    """
    if word in vocabulary:
        return True
    if len(word) < 4:
        return False
    return any(
        len(known) >= 4 and (word.startswith(known) or known.startswith(word))
        for known in vocabulary
    )


def check_stored(title: str, source: str, stored: str) -> list[Mismatch]:
    """Report the lines of *stored* that do not trace back to *source*."""
    vocabulary = source_vocabulary(source)
    mismatches = []
    for line in stored.splitlines():
        # The title is echoed above the workout body and is not a movement. It can
        # differ from ours by spacing alone — "EMF 60 :  Handstand Walk" keeps the
        # source's double space on one side and not the other.
        if not line.strip() or " ".join(line.split()) == " ".join(title.split()):
            continue
        if not _PRESCRIPTION_LINE.match(line):
            continue
        words = _words(line)
        if not words:
            continue
        grounded = sum(1 for w in words if _traces_back(w, vocabulary)) / len(words)
        if grounded < GROUNDED_FRACTION:
            mismatches.append(Mismatch(title=title, stored=line.strip(), grounded=grounded))
    return mismatches
