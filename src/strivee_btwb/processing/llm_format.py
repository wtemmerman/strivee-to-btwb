"""LLM-based workout formatting for BTWB.

Replaces the regex approach for the post step: sends each block's raw content to
a local Ollama model, which extracts the Rx-level content and formats it cleanly
for entry into BTWB's workout form.
"""

import logging
import re

import ollama

from ..core import config
from ..core.models import ProgrammingBlock

logger = logging.getLogger("processing")

_BTWB_EXAMPLES = """\
3 rounds for time:
Row 500m
21 Box Jumps 24/20
12 Burpees


[next]
21-15-9 for time:
Thruster 95lbs
Pull-ups


[next]
50-40-30-20-10 for time:
DB Snatch
Burpees


[next]
10-9-8-7-6-5-4-3-2-1 for time:
Deadlift 1.5xBW
Bench 1xBW
Clean 0.75xBW


[next]
For time:
3 rounds:
10 Snatch 95/65
12 Bar Facing Burpees
Rest 3 min
3 rounds:
10 Bar Muscle-ups
12 Bar Facing Burpees


[next]
5 rounds:
20 Pull-ups
30 Push-ups
40 Sit-ups
50 Air Squats
Rest 3 min


[next]
3 rounds quality:
10 Pull-ups
10 Push-ups
10 Sit-ups
10 Air Squats


[next]
EMOM 30:
5 Pull-ups
10 Push-ups
15 Air Squats


[next]
E4MOM 20:
400m Run
15 Wall Balls 20/14
10 Burpees


[next]
EMOM 10 alt:
10 Bike cals
50 DU


[next]
AMRAP 20:
5 Pull-ups
10 Push-ups
15 Box Jumps 24


[next]
5 cycles:
3-min AMRAP:
3 Power Cleans 135/95
6 Push-ups
9 Air Squats
Rest 1 min


[next]
AMRAP 20:
400m Run
Max Pull-ups


[next]
20 min:
1 mile run
then AMRAP:
5 Pull-ups
10 Push-ups
15 Air Squats


[next]
7 min ladder AMRAP:
Thruster 100/65 + CTB Pull-ups
+3 reps each round


[next]
AMRAP 2:
Double Unders


[next]
Tabata:
Pull-up / Push-up / Sit-up / Air Squat
20s on 10s off x8
Score: total reps


[next]
Tabata block:
Row / Air Squat / Pull-up / Push-up / Sit-up
Rest 1 min between
Score: lowest interval reps


[next]
3 rounds (1 min stations):
Wall Ball 20/14
SDHP 75/55
Box Jump 20
Push Press 75/55
Row cals
Rest 1 min


[next]
Ladder (continuous clock):
1 DL + 1 BJ
2 DL + 2 BJ
3 DL + 3 BJ
...


[next]
Ladder:
1 Pull-up
2 Pull-ups
3 Pull-ups
...


[next]
Ladder DL:
3 reps each min, add +10lb each round
...


[next]
3 rounds (3 min):
15 HPCJ 115/75
Max Row cals
Rest 1:30


[next]
5 rounds:
Max Bench 1xBW
Max Pull-ups


[next]
Deadlift 5-5-5-5-5 heavy


[next]
Deadlift 5x5 same weight
Rest 3 min"""

_PROMPT = """\
You are formatting a CrossFit workout block for entry into Beyond The Whiteboard (BTWB).

KEEP — the workout prescription only:
- Sets, reps, weights, distances, times, movements
- Work/rest intervals and round structure

FORMAT — apply these substitutions:
- Percentages of 1RM use @ not #: "#70%" → "@70%", "#83%" → "@83%"
- Weights keep # as-is: "#50/35kg", "#43kg" stay unchanged

REMOVE everything else, including:
- Athlete level headers and everything under them: when a block contains sections like "RX 🏋️", "INTER+", "INTER 🏆" (with or without emoji), output ONLY the content of the first/top section. Discard the header line itself and all content that follows the first sub-level heading.
- Objectives and coaching intent: lines starting with "Objectif", "But", "l'objectif", "On cherche", "On veut", "On reprend", "Semaine X/Y", "Week X/Y", "Progression :"
- Technique cues and motivational text: lines with "RPE", "Technique", "Focus", "Montez", "Gardez", "Essayez", "Tension", "Vitesse", "Pensez", "Contrôle", or similar coaching language
- Parenthetical coaching notes: lines like "(RPE 7-8 — pas un max absolu)"
- All-caps shouts ending with "!": e.g. "DÉPART EN CLEAN AND JERK OBLIGATOIRE AMPLITUDE COMPLÈTE !"
- Score labels and UI artifacts: lines like "Score :", "Modifier", "X Scores", "X Media", "Noter", "Commenter"

Keep the original language (French or English). Do not translate.
Output ONLY the formatted workout text. No preamble, no labels, no markdown, no separators.

Level-stripping example:
INPUT:
Nouveau format !
RX 🏋️
Every 4 min x 3 rounds:
12/10 Cal Row
6/4 Ring Muscle-up
INTER+ 🏃
Every 4 min x 3 rounds:
12/10 Cal Row
4 Bar Muscle-up
INTER 🏆
Every 4 min x 3 rounds:
12/10 Cal Row
8 Chest to bar pull-up
OUTPUT:
Every 4 min x 3 rounds:
12/10 Cal Row
6/4 Ring Muscle-up

BTWB format style examples (do NOT copy these into your output):
{examples}

Workout block to format:
{content}"""


def format_for_btwb(block: ProgrammingBlock, model: str | None = None) -> ProgrammingBlock:
    """Reformat a block's content for BTWB using a local Ollama model.

    Falls back to regex-based prepare_block if the LLM returns empty content.
    """
    m = model or config.OLLAMA_FORMAT_MODEL
    logger.debug("[%s] formatting with model '%s'", block.name, m)
    logger.debug("[%s] input (%d chars):\n%s", block.name, len(block.content), block.content)

    prompt = _PROMPT.format(examples=_BTWB_EXAMPLES, content=block.content)
    logger.debug("[%s] prompt (%d chars):\n%s", block.name, len(prompt), prompt)
    try:
        response = ollama.chat(
            model=m,
            messages=[{"role": "user", "content": prompt}],
            options={"temperature": 0},
            think=False,
        )
        result = response["message"]["content"].strip()
        result = re.sub(r"#(\d+(?:\.\d+)?)%", r"@\1%", result)
        if result:
            logger.debug("[%s] output (%d chars):\n%s", block.name, len(result), result)
            return ProgrammingBlock(name=block.name, content=result, instruction=block.instruction)
        logger.warning("[%s] LLM returned empty — returning original content", block.name)
    except Exception as exc:
        logger.warning("[%s] LLM format error (%s) — returning original content", block.name, exc)
    return block
