# Strivee to Beyond The White Board synchronisation

Automates the weekly transfer of CrossFit programming from the **Strivee** Android app to **Beyond The Whiteboard (BTWB)**.

## Coverage

![Coverage](https://img.shields.io/badge/coverage-88%25-brightgreen?style=flat&logo=pytest)

> Run `make test-cov` to regenerate with an HTML report in `htmlcov/`.

---

## Goal

Strivee is the app used by the gym to publish the weekly programming (strength, WODs, accessories). BTWB is the platform athletes use to log their workouts. Every Monday, the programming must be manually re-entered into BTWB block by block — this tool automates that entire process.

---

## How It Works

The pipeline runs in four independent steps, each caching its output so any step can be re-run without repeating earlier work.

```
Android phone (Strivee app)
        │
        │  ADB over USB — UI accessibility text dump
        ▼
  1. capture   → captures/<week>/strivee_<ts>_<day>.txt
        │
        │  qwen3:8b — extract blocks from raw UI text
        ▼
  2. analyse   → parsed/<week>/parsed_<date>_<day>.json
        │
        │  qwen3:8b — format for BTWB (same model, same quality)
        ▼
  3. preview   → terminal log (content + coaching notes, review before posting)
        │
        │  Playwright browser automation
        ▼
  4. post      → workouts + coaching notes created on BTWB
```

### Step 1 — Capture

Connects to the Android phone via ADB, launches Strivee, navigates to each day tab, and uses `adb shell uiautomator dump` at each scroll position to extract all visible text from Android's accessibility tree. Text elements are deduplicated across scroll positions. Saves one `.txt` file per day — no screenshots, no stitching, no overlap possible.

<details>
<summary>Example: <code>captures/2026-05-04/strivee_20260505_120721_Mon.txt</code></summary>

```

EMF 60'
LUN
4

MAR
5
MER
6
JEU
7
VEN
8
SAM
9
DIM
10
EMF Rx : Hebdomadaire
🚨 🚨 🚨 CALL HEBDOMADAIRE JEUDI 07/01/26 🚨 🚨 🚨

CALL HEBDOMADAIRE a 18h
...
GROUPE WHATS APP EMF RX
HELLO ! Nouveau sur la prog ?
...
WOD

Box

Noter

PRs

Profil
🔥 Warm-up 🔥
➡️ KB Ankle Stretch : x1min30/side
➡️ 60 Sec/side Adductor stretch with hip rotation
➡️ 60 sec Hand on floor Cossack squat

Muscle Snatch from hip
Hang Muscle Snatch
Low hang Muscle Snatch
Hang Power Snatch
Low Hang Power Snatch

x6 reps / movement
EMF 60 : Snatch
Build to a 1RM Squat Snatch for the day

Gamme suggéré :

Warm-up : 60% / 70% / 75% / 80% #2-3 reps

Build - 1 Rep : @83% / @86% / @88% / @90%

Tentatives lourdes : @93% / @95% / @97% / New RM (si sensation)

Objectif : C'est le jour J du snatch. 8 semaines de progression vers ce moment.
Montez progressivement, ne brûlez pas les étapes. Essayez de respecter les grandes phases
du mouvement avant de vouloir mettre beaucoup de vitesse !

Si vous ratez 3 fois la même charge : arrêtez-vous.

Modifier

11 Scores
EMF 60 - Gymnastic Ring Muscle-up
RX 🔱

AMRAP 5:00
Max sets of 4 Ring Muscle-up Unbroken

Score : total sets / total RMU

Objectif : Format simple, l'objectif n'est pas de vous détruire avant la dernière partie de la séance !
Restez propre et efficace.

INTER+ 🪖
AMRAP 5:00
Max sets of 2 Bar Muscle-up Unbroken

INTER 🎖️
High amplitude ring swing focus x40 reps
Kipping hip extension x12 reps
Bascule feet on the floor x12 reps

+

6-10 Ring Muscle-up
Practice !

3 Media

4 Scores
EMF 60 - ITW Gymnastic X Odd objectif
🔱 Rx - 🪖 INTER+ -

AMRAP 12:00
24 Double Under's
6 Power clean #50/35kg
6 Strict HSPU

🎖️ INTER -

AMRAP 12:00
24 Double Under's
6 Power clean #40/30kg
6 Strict HSPU with abmat

Objectif : DU's unbroken - power clean Tng en 2 séries max - strict HSPU 2 séries max !
9 Scores
```

The raw dump contains navigation chrome (day tabs, bottom bar), excluded blocks (Hebdomadaire, WhatsApp links, Warm-up), UI labels (Modifier, Scores, Media), and multiple athlete levels (RX/INTER+/INTER). The analyse step strips all of this.

</details>

### Step 2 — Analyse

Sends each day's text dump to a local Ollama text model (`qwen3:8b`). The model extracts every programming block by name, content (RX workout prescription only), and instruction (coaching notes + alternate levels) and returns structured JSON. The output JSON has three fields per block: `name`, `content`, `instruction`.

**Example output** (`parsed/2026-04-27/parsed_2026-04-27_Mon.json`):

```json
{
  "date": "2026-04-27",
  "day_label": "Mon",
  "blocks": [
    {
      "name": "Squat Snatch",
      "content": "EMOMx 8 sets:\nSet 1 à 4: 2 Squat Snatch @70-73% of your 1RM\nSet 5 à 8: 1 Squat Snatch @75-83% of your 1RM",
      "instruction": "Objectif: focus on positions"
    },
    {
      "name": "WOD",
      "content": "AMRAP 12:00\n10 Thrusters #43/29kg\n10 Pull-ups",
      "instruction": ""
    }
  ]
}
```

### Step 3 — Preview

Loads the cached JSON, runs the same LLM formatting as the post step, and prints the result for review. What you see is exactly what will be submitted to BTWB: the formatted prescription (`content`) and the coaching note (`instruction`) shown separately.

<details>
<summary>Example preview output (Monday)</summary>

```
============================================================
  BTWB Preview — Week starting 2026-05-04
============================================================
  MON — 2026-05-04  (3 block(s))
  [EMF 60 : Snatch]
      Build to a 1RM Squat Snatch for the day
    ── coaching note ──
      Gamme suggéré :

      Warm-up : 60% / 70% / 75% / 80% #2-3 reps

      Build - 1 Rep : @83% / @86% / @88% / @90%

      Tentatives lourdes : @93% / @95% / @97% / New RM (si sensation)

      Objectif : C'est le jour J du snatch. 8 semaines de progression vers ce moment.
      Montez progressivement, ne brûlez pas les étapes.

      Si vous ratez 3 fois la même charge : arrêtez-vous.
  [EMF 60 - Gymnastic Ring Muscle-up]
      AMRAP 5:00
      Max sets of 4 Ring Muscle-up Unbroken
    ── coaching note ──
      Score : total sets / total RMU

      Objectif : Format simple, l'objectif n'est pas de vous détruire avant la dernière partie de la séance !
      Restez propre et efficace.

      🪖 INTER+ 🪖
      AMRAP 5:00
      Max sets of 2 Bar Muscle-up Unbroken

      🎖️ INTER -
      High amplitude ring swing focus x40 reps
      Kipping hip extension x12 reps
      Bascule feet on the floor x12 reps

      +

      6-10 Ring Muscle-up
      Practice !
  [EMF 60 - ITW Gymnastic X Odd objectif]
      AMRAP 12:00
      24 Double Under's
      6 Power clean #50/35kg
      6 Strict HSPU
    ── coaching note ──
      🎖️ INTER -

      AMRAP 12:00
      24 Double Under's
      6 Power clean #40/30kg
      6 Strict HSPU with abmat

      Objectif : DU's unbroken - power clean Tng en 2 séries max - strict HSPU 2 séries max !
```

The `content` field (prescription) is posted to the BTWB workout description. The `instruction` field (coaching notes + alternate levels for INTER+/INTER) is posted to the BTWB coaching note field.

</details>

### Step 4 — Post

Opens a Playwright browser session, logs into BTWB, and submits each block via the planning form. Blocks already present on BTWB for that date are skipped automatically (duplicate detection via the weekly calendar). The `instruction` field is posted to BTWB's dedicated coaching note field.

<details>
<summary>Result on BTWB</summary>

![BTWB calendar](docs/screenshots/btwb_calendar.png)

</details>

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.13+ | Managed by `uv` |
| [uv](https://docs.astral.sh/uv/) | Package and environment manager |
| [Ollama](https://ollama.com) | Local text model runtime |
| ADB | `brew install android-platform-tools` |
| USB debugging | Enabled on the Android device |
| scrcpy _(optional)_ | Visual mirror during capture — `brew install scrcpy` |

Pull the model once:

```bash
ollama pull qwen3:8b          # text — analyse, preview, and post formatting
```

---

## Installation

```bash
git clone https://github.com/wtemmerman/strivee-btwb.git
cd strivee-btwb
make dev-install
cp .env.example .env
# Edit .env with your BTWB credentials and Ollama model
```

---

## Configuration

Copy `.env.example` to `.env` and fill in the required values:

```env
OLLAMA_TEXT_MODEL=qwen3:8b         # text model for analyse step
OLLAMA_FORMAT_MODEL=qwen3:8b       # text model for preview/post formatting (same model)

BTWB_EMAIL=your@email.com
BTWB_PASSWORD=yourpassword
BTWB_TRACK_ID=156552        # visible in BTWB calendar URL: ?t=<id>

# Blocks to skip (case-insensitive substring match)
EXCLUDED_BLOCKS=Hebdomadaire,GROUPE WHATS APP EMF,Warm-up
```

---

## Usage

Run the full pipeline for the current week:

```bash
uv run strivee-btwb run --yes
```

Or step by step:

```bash
# Step 1 — capture all days (Mon–Sat by default) via UI text dump
uv run strivee-btwb capture

# Step 2 — analyse with text model
uv run strivee-btwb analyse

# Step 3 — preview what will be posted
uv run strivee-btwb preview

# Step 4 — post to BTWB (prompts for confirmation)
uv run strivee-btwb post
```

### Flags available on all commands

```bash
--days Mon,Tue,Wed        # process specific days only
--week 2026-04-20         # target a specific week (any date in the week); defaults to current week
--debug                   # verbose logging
```

### Additional flags

```bash
capture --no-scrcpy       # skip launching the screen mirror
post    --yes             # skip interactive confirmation
post    --headless        # run browser without a visible window
```

### Examples

```bash
# Re-run the full pipeline on a past week for testing
uv run strivee-btwb run --week 2026-04-20 --yes

# Analyse and post a specific day from a previous week
uv run strivee-btwb analyse --week 2026-04-20 --days Mon
uv run strivee-btwb post    --week 2026-04-20 --days Mon --yes
```

---

## Development

```bash
make test             # run all tests
make test-cov         # tests with HTML coverage report (htmlcov/)
make lint             # ruff lint check
make format           # ruff format + import sort
```

### Project Structure

```
src/strivee_btwb/
  core/           config, logging setup, data models
  capture/        ADB UI accessibility text dump (adb.py)
  vision/         Ollama text parsing — block extraction (parser.py)
  processing/     LLM-based BTWB formatting — Rx extraction, coaching strip (llm_format.py)
  btwb/           BTWB Playwright automation (client.py)
  pipeline.py     step orchestration and cache I/O
  cli.py          argparse wiring
  __main__.py     entry point

tests/
  unit/
    core/           model tests
    capture/        UI text helpers, element detection, capture_day_as_text
    vision/         JSON extraction, mock Ollama tests
    processing/     Rx extraction, coaching strip
    btwb/           dry-run posting
    test_pipeline   cache I/O, week processing
    test_cli        argument parsing
  fixtures/
    2026-04-27/     real parsed JSON used as test data
```

### Runtime directories (gitignored)

| Directory | Contents |
|---|---|
| `captures/<week>/` | UI text dumps (.txt) |
| `parsed/<week>/` | Text-parsed JSON cache |
| `htmlcov/` | Coverage HTML report |

---

## Design Decisions

### Approach history

| Approach | Result |
|---|---|
| **Qwen2.5-VL (vision)** | Accurate but slow and VRAM-heavy (~15 GB at 8k context) |
| **OCR + LLM** | Fast but poor accuracy — OCR errors compounded into unreliable extraction |
| **Qwen3-VL (vision)** | Better than Qwen2.5-VL but overlap in stitched screenshots caused duplicate content |
| **ADB UI text dump + Qwen3:8b** | Current — zero overlap possible, faster than vision, no VRAM for image processing |

**Hard constraint:** no cloud APIs (zero cost). Every model must run locally via Ollama.

Cloud vision APIs (Claude, GPT-4o) were never tested — they would give better accuracy but introduce per-run cost and a network dependency, which is a non-starter for a weekly personal automation.

### Text model approach

The text model (`qwen3:8b`) receives the raw accessibility-tree text for one day and returns structured JSON. It uses `think=False` to suppress thinking tokens and ensure the visible output is always the JSON response directly.

### LLM-based BTWB formatting

After text parsing, each block's `content` (prescription only) is sent to `OLLAMA_FORMAT_MODEL` (`qwen3:8b`) before preview and post. The same model is used for both analyse and format steps — `qwen3:8b` gives reliable output quality for text fidelity; smaller models (e.g. 1.7b) hallucinate movements and leak thinking-token artifacts. Since the text parser already separates content from `instruction` (coaching notes), the formatting model works on already-clean prescription text. The model:

1. Keeps only the RX / top-performance section when multiple athlete levels are present (RX, Inter+, Inter, etc.)
2. Removes Strivee UI artifacts (score labels, media counts, etc.)

The `instruction` field is posted directly to BTWB's dedicated coaching note field without further transformation.

If the model returns an empty response the original block content is kept unchanged, so the pipeline never silently drops content.
