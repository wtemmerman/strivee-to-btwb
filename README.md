# Strivee to Beyond The White Board synchronisation

Automates the weekly transfer of CrossFit programming from the **Strivee** Android app to **Beyond The Whiteboard (BTWB)**.

## Coverage

![Coverage](https://img.shields.io/badge/coverage-84%25-brightgreen?style=flat&logo=pytest)

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
        │  ADB over USB
        ▼
  1. capture   → captures/<week>/strivee_<ts>_<day>.png
        │
        │  Ollama vision model (local, no API cost)
        ▼
  2. analyse   → parsed/<week>/parsed_<date>_<day>.json
        │
        │  Rx extraction + coaching-note stripping
        ▼
  3. preview   → terminal log (review before posting)
        │
        │  Playwright browser automation
        ▼
  4. post      → workouts created on BTWB
```

### Step 1 — Capture

Connects to the Android phone via ADB, launches Strivee, navigates to each day tab, and scrolls down capturing screenshots. Frames are stitched into one image per day.

<details>
<summary>Example capture — Monday 2026-04-27</summary>

![Monday capture](docs/screenshots/capture_monday.png)

</details>

### Step 2 — Analyse

Sends each day's stitched screenshot to a local Ollama vision model (`qwen3-vl:8b`). The model extracts every programming block by name and content and returns structured JSON. If the primary model returns no blocks (e.g. due to thinking tokens consuming all compute), the pipeline automatically retries with a configurable fallback model (`qwen2.5vl:7B`) before giving up.

**Example output** (`parsed/2026-04-27/parsed_2026-04-27_Mon.json`):

```json
{
  "date": "2026-04-27",
  "day_label": "Mon",
  "blocks": [
    {
      "name": "Squat Snatch",
      "content": "EMOMx 8 sets:\nSet 1 à 4: 2 Squat Snatch @70-73% of your 1RM\nSet 5 à 8: 1 Squat Snatch @75-83% of your 1RM"
    },
    {
      "name": "Gymnastic Ring Muscle-up",
      "content": "RX:\nEvery 2min x 4 sets:\n10/7 Cal Echo Bike @85%+\nMax Ring Muscle-up Unbroken"
    },
    {
      "name": "ITW Gymnastic X Odd objectif",
      "content": "Rx -\n2 Rounds for time:\n25 Dumbbell snatch #22,5/15\n25 Chest to bar pull-up\n25 Box jump over\nTime CAP: 10:00"
    }
  ]
}
```

### Step 3 — Preview

Loads the cached JSON, runs the same LLM formatting as the post step, and prints the result for review. What you see is exactly what will be submitted to BTWB.

### Step 4 — Post

Opens a Playwright browser session, logs into BTWB, and submits each block via the planning form. Blocks already present on BTWB for that date are skipped automatically (duplicate detection via the weekly calendar).

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
| [Ollama](https://ollama.com) | Local vision model runtime |
| ADB | `brew install android-platform-tools` |
| USB debugging | Enabled on the Android device |
| scrcpy _(optional)_ | Visual mirror during capture — `brew install scrcpy` |

Pull the models once:

```bash
ollama pull qwen3-vl:8b       # vision — analyse step
ollama pull qwen2.5vl:7B      # vision fallback (used when primary returns 0 blocks)
ollama pull qwen3.5:2b        # text — preview and post formatting
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
OLLAMA_MODEL=qwen3-vl:8b             # vision model for analyse step
OLLAMA_FALLBACK_MODEL=qwen2.5vl:7B   # retried automatically when primary returns 0 blocks
OLLAMA_FORMAT_MODEL=qwen3.5:2b       # small text model for preview/post formatting

BTWB_EMAIL=your@email.com
BTWB_PASSWORD=yourpassword
BTWB_TRACK_ID=156552        # visible in BTWB calendar URL: ?t=<id>

MAX_SCROLLS=10
SCROLL_DISTANCE=0.42

# Crop the Strivee header and nav bar from each frame (tune to your device)
CAPTURE_CROP_TOP=550
CAPTURE_CROP_BOTTOM=250

# Blocks to skip (case-insensitive prefix match)
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
# Step 1 — capture all days (Mon–Sat by default)
uv run strivee-btwb capture

# Step 2 — analyse with vision model
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
  capture/        ADB screenshot capture (adb.py)
  vision/         Ollama vision parsing (parser.py)
  processing/     LLM-based BTWB formatting — Rx extraction, coaching strip (llm_format.py)
  btwb/           BTWB Playwright automation (client.py)
  pipeline.py     step orchestration and cache I/O
  cli.py          argparse wiring
  __main__.py     entry point

tests/
  unit/
    core/           model tests
    capture/        image helpers, crop, UI element detection
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
| `captures/<week>/` | Raw ADB screenshots (PNG) |
| `parsed/<week>/` | Vision-parsed JSON cache |
| `htmlcov/` | Coverage HTML report |

---

## Design Decisions

### Approach history

| Approach | Result |
|---|---|
| **Qwen2.5-VL (vision-only)** | Accurate but slow and VRAM-heavy (~15 GB at 8k context) |
| **OCR + LLM** | Fast but accuracy was poor — OCR errors compounded into the LLM input and produced unreliable block extraction |
| **Qwen3-VL (vision-only)** | ✅ Current primary — faster than Qwen2.5-VL, ~11 GB at 32k context, same accuracy |

**Hard constraint:** no cloud APIs (zero cost). Every model must run locally via Ollama.

Cloud vision APIs (Claude, GPT-4o) were never tested — they would give better accuracy but introduce per-run cost and a network dependency, which is a non-starter for a weekly personal automation.

### Primary → fallback model chain

Qwen3-VL has a "thinking" mode that, on some days with dense content, spends all available compute on internal reasoning tokens and returns an empty response. To make the pipeline resilient without switching models entirely, `analyse` implements a two-stage retry:

1. Run the primary model (`OLLAMA_MODEL`, default `qwen3-vl:8b`) with `think=False` to suppress thinking tokens.
2. If the result contains **0 blocks**, automatically retry with `OLLAMA_FALLBACK_MODEL` (default `qwen2.5vl:7B`).
3. If the fallback also returns 0 blocks, log a warning and skip that day.

The fallback is optional — leave `OLLAMA_FALLBACK_MODEL` unset to disable it.

### LLM-based BTWB formatting

After vision parsing, each block's raw content is sent to a small local text model (`OLLAMA_FORMAT_MODEL`, default `qwen3.5:2b`) before preview and post. The model:

1. Keeps only the RX / top-performance section when multiple athlete levels are present (RX, Inter+, Inter, etc.)
2. Strips coaching notes, technique cues, objectives, and motivational text
3. Removes Strivee UI artifacts (score labels, media counts, etc.)

A regex-based approach was tried first but proved too fragile — coaches use inconsistent formatting, emoji after level headers, mixed French/English, and new patterns appear every week. The LLM handles all of these naturally.

If the model returns an empty response the original block content is kept unchanged, so the pipeline never silently drops content.
