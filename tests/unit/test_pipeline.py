"""Unit tests for pipeline cache I/O and week-processing functions."""

import json
import logging
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from strivee_btwb.core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming
from strivee_btwb.pipeline import (
    clean_week,
    do_analyse,
    do_capture,
    do_post,
    do_preview,
    load_days,
    load_text_captures,
    log_preview,
    log_summary,
    save_day,
    save_text_capture,
    short_to_date,
    week_start,
)

FIXTURE_WEEK = date(2026, 4, 27)
FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_day(
    label: str = "Mon",
    day_date: date = date(2026, 4, 27),
    blocks: list[ProgrammingBlock] | None = None,
) -> DayProgramming:
    return DayProgramming(
        date=day_date,
        day_label=label,
        blocks=blocks or [ProgrammingBlock(name="Back Squat", content="5x5 @ 80%")],
    )


def _make_week(*days: DayProgramming) -> WeeklyProgramming:
    return WeeklyProgramming(week_start=FIXTURE_WEEK, days=list(days))


# ── save_day / load_days ──────────────────────────────────────────────────────


def test_save_day_creates_json(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    day = _make_day()
    path = save_day(day, FIXTURE_WEEK)

    assert path.exists()
    assert path.suffix == ".json"
    data = json.loads(path.read_text())
    assert data["date"] == "2026-04-27"
    assert data["day_label"] == "Mon"
    assert data["blocks"][0]["name"] == "Back Squat"


def test_save_day_round_trips(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    day = _make_day(blocks=[ProgrammingBlock(name="WOD", content="21-15-9\nThrusters")])
    save_day(day, FIXTURE_WEEK)

    week = load_days(["Mon"], FIXTURE_WEEK)
    assert len(week.days) == 1
    assert week.days[0].blocks[0].content == "21-15-9\nThrusters"


def test_load_days_reads_real_fixture(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    week = load_days(["Mon", "Tue"], FIXTURE_WEEK)

    assert len(week.days) == 2
    assert week.days[0].day_label == "Mon"
    assert week.days[1].day_label == "Tue"
    assert any(b.name == "Squat Snatch" for b in week.days[0].blocks)
    assert any("Run Session" in b.name for b in week.days[1].blocks)


def test_load_days_warns_on_missing(tmp_path, monkeypatch, caplog):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    (tmp_path / FIXTURE_WEEK.isoformat()).mkdir()

    with caplog.at_level(logging.WARNING):
        week = load_days(["Mon"], FIXTURE_WEEK)

    assert week.days == []
    assert "Mon" in caplog.text


# ── clean_week ────────────────────────────────────────────────────────────────


def test_clean_week_removes_empty_blocks():
    week = _make_week(
        _make_day(
            blocks=[
                ProgrammingBlock(name="Back Squat", content="5x5"),
                ProgrammingBlock(name="Empty", content="   "),
            ]
        )
    )
    result = clean_week(week)
    assert len(result.days[0].blocks) == 1
    assert result.days[0].blocks[0].name == "Back Squat"


def test_clean_week_merges_consecutive_same_name():
    week = _make_week(
        _make_day(
            blocks=[
                ProgrammingBlock(name="WOD", content="Part A"),
                ProgrammingBlock(name="WOD", content="Part B"),
            ]
        )
    )
    result = clean_week(week)
    assert len(result.days[0].blocks) == 1
    assert "Part A" in result.days[0].blocks[0].content
    assert "Part B" in result.days[0].blocks[0].content


def test_clean_week_does_not_merge_different_names():
    week = _make_week(
        _make_day(
            blocks=[
                ProgrammingBlock(name="Strength", content="5x5"),
                ProgrammingBlock(name="WOD", content="21-15-9"),
            ]
        )
    )
    result = clean_week(week)
    assert len(result.days[0].blocks) == 2


def test_clean_week_drops_day_when_all_blocks_empty():
    week = _make_week(_make_day(blocks=[ProgrammingBlock(name="Rest", content="  ")]))
    result = clean_week(week)
    assert result.days == []


def test_clean_week_merge_is_case_insensitive():
    week = _make_week(
        _make_day(
            blocks=[
                ProgrammingBlock(name="wod", content="Part A"),
                ProgrammingBlock(name="WOD", content="Part B"),
            ]
        )
    )
    result = clean_week(week)
    assert len(result.days[0].blocks) == 1


# ── week_start / short_to_date ────────────────────────────────────────────────


def test_week_start_returns_monday():
    ws = week_start()
    assert ws.weekday() == 0


def test_short_to_date_mon():
    ws = week_start()
    assert short_to_date("Mon") == ws


def test_short_to_date_sat():
    ws = week_start()
    sat = short_to_date("Sat")
    assert (sat - ws).days == 5


# ── log_summary / log_preview ─────────────────────────────────────────────────


def test_log_summary_logs_week_info(caplog):
    week = _make_week(_make_day())
    with caplog.at_level(logging.INFO):
        log_summary(week)
    assert "2026-04-27" in caplog.text
    assert "MON" in caplog.text
    assert "Back Squat" in caplog.text


def test_log_preview_logs_block_content(caplog):
    week = _make_week(_make_day(blocks=[ProgrammingBlock(name="WOD", content="21-15-9")]))
    with caplog.at_level(logging.INFO):
        log_preview(week)
    assert "WOD" in caplog.text
    assert "21-15-9" in caplog.text


# ── do_preview ────────────────────────────────────────────────────────────────


def test_do_preview_success(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    monkeypatch.setattr("strivee_btwb.pipeline.format_for_btwb", lambda block, **_: block)
    do_preview(["Mon", "Tue"], ws=FIXTURE_WEEK)


def test_do_preview_exits_when_no_cache(monkeypatch, tmp_path):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    (tmp_path / FIXTURE_WEEK.isoformat()).mkdir()
    with pytest.raises(SystemExit):
        do_preview(["Mon"])


# ── do_analyse ────────────────────────────────────────────────────────────────


def test_do_analyse_success(monkeypatch, tmp_path):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    (folder / "strivee_20260427_120000_Mon.txt").write_text(
        "EMF 60 : Snatch\nBuild to 1RM", encoding="utf-8"
    )

    fake_day = _make_day()
    monkeypatch.setattr(
        "strivee_btwb.pipeline.extract_day_programming_from_text",
        lambda **_: fake_day,
    )
    do_analyse(["Mon"], ws=FIXTURE_WEEK)
    assert any((folder).glob("parsed_*_Mon.json"))


def test_do_analyse_exits_when_no_captures(monkeypatch, tmp_path):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    (tmp_path / FIXTURE_WEEK.isoformat()).mkdir()
    with pytest.raises(SystemExit):
        do_analyse(["Mon"])


def test_do_analyse_logs_warning_on_empty_blocks(monkeypatch, tmp_path, caplog):
    import strivee_btwb.core.config as cfg
    from strivee_btwb.core.models import DayProgramming

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    (folder / "strivee_20260427_120000_Mon.txt").write_text("some text", encoding="utf-8")

    empty_day = DayProgramming(date=date(2026, 4, 27), day_label="Mon", blocks=[])
    monkeypatch.setattr(
        "strivee_btwb.pipeline.extract_day_programming_from_text",
        lambda **_: empty_day,
    )
    with caplog.at_level(logging.WARNING):
        do_analyse(["Mon"], ws=FIXTURE_WEEK)
    assert "no blocks" in caplog.text.lower()


# ── do_capture ────────────────────────────────────────────────────────────────


def test_do_capture_success(monkeypatch, tmp_path):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ANDROID_SERIAL", None)
    monkeypatch.setattr(cfg, "MAX_SCROLLS", 2)
    monkeypatch.setattr("strivee_btwb.pipeline.launch_scrcpy", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.launch_strivee", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.navigate_to_week", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.scroll_to_top", MagicMock())
    monkeypatch.setattr(
        "strivee_btwb.pipeline.capture_day_as_text",
        lambda *a, **k: "EMF 60 : Snatch\nBuild to 1RM",
    )

    do_capture(["Mon"], no_scrcpy=True, ws=FIXTURE_WEEK)

    # text file should be created
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    assert any(folder.glob("strivee_*_Mon.txt"))


def test_do_capture_exits_when_strivee_fails(monkeypatch, tmp_path):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "ANDROID_SERIAL", None)
    monkeypatch.setattr(
        "strivee_btwb.pipeline.launch_strivee", MagicMock(side_effect=RuntimeError("no device"))
    )
    with pytest.raises(SystemExit):
        do_capture(["Mon"], no_scrcpy=True)


def test_do_capture_exits_when_no_days_saved(monkeypatch, tmp_path):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ANDROID_SERIAL", None)
    monkeypatch.setattr(cfg, "MAX_SCROLLS", 1)
    monkeypatch.setattr("strivee_btwb.pipeline.launch_strivee", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.navigate_to_week", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.scroll_to_top", MagicMock())
    monkeypatch.setattr(
        "strivee_btwb.pipeline.capture_day_as_text",
        MagicMock(side_effect=RuntimeError("adb error")),
    )
    with pytest.raises(SystemExit):
        do_capture(["Mon"], no_scrcpy=True)


# ── do_post ───────────────────────────────────────────────────────────────────


def test_do_post_success(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    monkeypatch.setattr(cfg, "BTWB_EMAIL", "test@example.com")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "password")

    mock_post = MagicMock(return_value=[{"block": "Squat Snatch", "ok": True}])
    monkeypatch.setattr("strivee_btwb.pipeline.post_week", mock_post)
    monkeypatch.setattr("strivee_btwb.pipeline.format_for_btwb", lambda block, **_: block)

    do_post(["Mon"], yes=True, headless=True, ws=FIXTURE_WEEK)
    mock_post.assert_called_once()


def test_do_post_exits_when_no_cache(monkeypatch, tmp_path):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    monkeypatch.setattr(cfg, "BTWB_EMAIL", "x")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "x")
    (tmp_path / FIXTURE_WEEK.isoformat()).mkdir()
    with pytest.raises(SystemExit):
        do_post(["Mon"], yes=True, headless=True)


def test_do_post_exits_without_credentials(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    monkeypatch.setattr(cfg, "BTWB_EMAIL", "")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "")
    with pytest.raises(SystemExit):
        do_post(["Mon"], yes=True, headless=True)


def test_do_post_exits_when_no_days_approved(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    monkeypatch.setattr(cfg, "BTWB_EMAIL", "x")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "x")
    monkeypatch.setattr("strivee_btwb.pipeline.format_for_btwb", lambda block, **_: block)

    # Simulate user saying "n" to all days
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with pytest.raises(SystemExit):
        do_post(["Mon"], yes=False, headless=True)


# ── save_text_capture / load_text_captures ────────────────────────────────────


def test_save_text_capture_creates_file(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    path = save_text_capture("some workout text", label="Mon", ws=FIXTURE_WEEK)
    assert path.exists()
    assert path.suffix == ".txt"
    assert "Mon" in path.name
    assert path.read_text() == "some workout text"


def test_save_text_capture_creates_week_subfolder(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    path = save_text_capture("text", label="Tue", ws=FIXTURE_WEEK)
    assert path.parent.name == FIXTURE_WEEK.isoformat()


def test_load_text_captures_reads_txt(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    (folder / "strivee_20260427_120000_Mon.txt").write_text("workout text", encoding="utf-8")

    result = load_text_captures(["Mon"], FIXTURE_WEEK)
    assert "Mon" in result
    assert result["Mon"] == "workout text"


def test_load_text_captures_picks_latest(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    (folder / "strivee_20260427_110000_Mon.txt").write_text("old text", encoding="utf-8")
    (folder / "strivee_20260427_120000_Mon.txt").write_text("new text", encoding="utf-8")

    result = load_text_captures(["Mon"], FIXTURE_WEEK)
    assert result["Mon"] == "new text"


def test_load_text_captures_missing_day_not_in_result(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    (tmp_path / FIXTURE_WEEK.isoformat()).mkdir()

    result = load_text_captures(["Mon"], FIXTURE_WEEK)
    assert "Mon" not in result


# ── do_capture additional paths ───────────────────────────────────────────────


def test_do_capture_scrcpy_not_found_logs_warning(monkeypatch, tmp_path, caplog):
    """FileNotFoundError from scrcpy is caught and logged as a warning."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ANDROID_SERIAL", None)
    monkeypatch.setattr(cfg, "MAX_SCROLLS", 1)
    monkeypatch.setattr(
        "strivee_btwb.pipeline.launch_scrcpy",
        MagicMock(side_effect=FileNotFoundError("scrcpy not found")),
    )
    monkeypatch.setattr("strivee_btwb.pipeline.launch_strivee", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.navigate_to_week", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.scroll_to_top", MagicMock())
    monkeypatch.setattr(
        "strivee_btwb.pipeline.capture_day_as_text",
        lambda *a, **k: "some text",
    )

    with caplog.at_level(logging.WARNING):
        do_capture(["Mon"], no_scrcpy=False, ws=FIXTURE_WEEK)

    assert "scrcpy" in caplog.text.lower()


def test_do_capture_terminates_scrcpy_after_success(monkeypatch, tmp_path):
    """scrcpy process is terminated even on success."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ANDROID_SERIAL", None)
    monkeypatch.setattr(cfg, "MAX_SCROLLS", 1)

    mock_proc = MagicMock()
    monkeypatch.setattr("strivee_btwb.pipeline.launch_scrcpy", MagicMock(return_value=mock_proc))
    monkeypatch.setattr("strivee_btwb.pipeline.launch_strivee", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.navigate_to_week", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.scroll_to_top", MagicMock())
    monkeypatch.setattr(
        "strivee_btwb.pipeline.capture_day_as_text",
        lambda *a, **k: "some text",
    )

    do_capture(["Mon"], no_scrcpy=False, ws=FIXTURE_WEEK)
    mock_proc.terminate.assert_called_once()


# ── do_analyse additional paths ───────────────────────────────────────────────


def test_do_analyse_fallback_model_used_on_empty_blocks(monkeypatch, tmp_path):
    """When primary returns no blocks and fallback model is set, fallback is tried."""
    import strivee_btwb.core.config as cfg
    from strivee_btwb.core.models import DayProgramming

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    monkeypatch.setattr(cfg, "OLLAMA_FALLBACK_TEXT_MODEL", "qwen3:fallback")
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    (folder / "strivee_20260427_120000_Mon.txt").write_text("some text", encoding="utf-8")

    call_count = {"n": 0}
    fake_day_empty = DayProgramming(date=date(2026, 4, 27), day_label="Mon", blocks=[])
    fake_day_full = _make_day()

    def fake_extract(**kwargs):
        call_count["n"] += 1
        # First call (primary) → empty; second call (fallback) → full
        return fake_day_empty if call_count["n"] == 1 else fake_day_full

    monkeypatch.setattr("strivee_btwb.pipeline.extract_day_programming_from_text", fake_extract)
    do_analyse(["Mon"], ws=FIXTURE_WEEK)
    assert call_count["n"] == 2
    assert any((folder).glob("parsed_*_Mon.json"))


def test_do_analyse_logs_error_on_exception(monkeypatch, tmp_path, caplog):
    """Exceptions in analysis are caught and logged without raising."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    (folder / "strivee_20260427_120000_Mon.txt").write_text("some text", encoding="utf-8")

    monkeypatch.setattr(
        "strivee_btwb.pipeline.extract_day_programming_from_text",
        MagicMock(side_effect=ValueError("model error")),
    )
    with caplog.at_level(logging.ERROR):
        do_analyse(["Mon"], ws=FIXTURE_WEEK)
    assert "analysis failed" in caplog.text.lower()


# ── do_post additional paths ──────────────────────────────────────────────────


def test_do_post_exits_on_auth_error(monkeypatch):
    """AuthenticationError from post_week causes sys.exit(1)."""
    import strivee_btwb.core.config as cfg
    from strivee_btwb.btwb import AuthenticationError

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    monkeypatch.setattr(cfg, "BTWB_EMAIL", "x@x.com")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "pw")
    monkeypatch.setattr("strivee_btwb.pipeline.format_for_btwb", lambda block, **_: block)
    monkeypatch.setattr(
        "strivee_btwb.pipeline.post_week",
        MagicMock(side_effect=AuthenticationError("bad creds")),
    )
    with pytest.raises(SystemExit):
        do_post(["Mon"], yes=True, headless=True, ws=FIXTURE_WEEK)


def test_do_post_exits_on_generic_exception(monkeypatch):
    """Any unexpected exception from post_week causes sys.exit(1)."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    monkeypatch.setattr(cfg, "BTWB_EMAIL", "x@x.com")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "pw")
    monkeypatch.setattr("strivee_btwb.pipeline.format_for_btwb", lambda block, **_: block)
    monkeypatch.setattr(
        "strivee_btwb.pipeline.post_week",
        MagicMock(side_effect=RuntimeError("network error")),
    )
    with pytest.raises(SystemExit):
        do_post(["Mon"], yes=True, headless=True, ws=FIXTURE_WEEK)


def test_do_post_approves_all_on_yes_input(monkeypatch):
    """When yes=False and user types 'y', all days are approved."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    monkeypatch.setattr(cfg, "BTWB_EMAIL", "x@x.com")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "pw")
    monkeypatch.setattr("strivee_btwb.pipeline.format_for_btwb", lambda block, **_: block)

    mock_post = MagicMock(return_value=[])
    monkeypatch.setattr("strivee_btwb.pipeline.post_week", mock_post)

    # First input is "y" → approve all days
    monkeypatch.setattr("builtins.input", lambda _: "y")
    do_post(["Mon"], yes=False, headless=True, ws=FIXTURE_WEEK)
    mock_post.assert_called_once()


def test_do_post_per_day_approval_loop(monkeypatch):
    """When 'n' is given to 'all', then 'y' to a single day, that day is posted."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    monkeypatch.setattr(cfg, "BTWB_EMAIL", "x@x.com")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "pw")
    monkeypatch.setattr("strivee_btwb.pipeline.format_for_btwb", lambda block, **_: block)

    mock_post = MagicMock(return_value=[])
    monkeypatch.setattr("strivee_btwb.pipeline.post_week", mock_post)

    # "n" to "Post all?", then "y" for Mon specifically
    responses = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    do_post(["Mon"], yes=False, headless=True, ws=FIXTURE_WEEK)
    mock_post.assert_called_once()


def test_do_post_per_day_approval_no_days_approved(monkeypatch):
    """When 'n' to all and 'n' to each day, exits with SystemExit."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    monkeypatch.setattr(cfg, "BTWB_EMAIL", "x@x.com")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "pw")
    monkeypatch.setattr("strivee_btwb.pipeline.format_for_btwb", lambda block, **_: block)

    # "n" to "Post all?", then "n" for each day
    monkeypatch.setattr("builtins.input", lambda _: "n")
    with pytest.raises(SystemExit):
        do_post(["Mon"], yes=False, headless=True, ws=FIXTURE_WEEK)


def test_do_post_exits_without_credentials_patched(monkeypatch):
    """Empty credentials cause sys.exit before posting."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    monkeypatch.setattr(cfg, "BTWB_EMAIL", "")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "")
    monkeypatch.setattr("strivee_btwb.pipeline.format_for_btwb", lambda block, **_: block)
    with pytest.raises(SystemExit):
        do_post(["Mon"], yes=True, headless=True, ws=FIXTURE_WEEK)


def test_do_capture_terminates_scrcpy_when_strivee_fails(monkeypatch, tmp_path):
    """scrcpy process is terminated when strivee fails to launch."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "ANDROID_SERIAL", None)
    mock_proc = MagicMock()
    monkeypatch.setattr("strivee_btwb.pipeline.launch_scrcpy", MagicMock(return_value=mock_proc))
    monkeypatch.setattr(
        "strivee_btwb.pipeline.launch_strivee",
        MagicMock(side_effect=RuntimeError("no device")),
    )
    with pytest.raises(SystemExit):
        do_capture(["Mon"], no_scrcpy=False, ws=FIXTURE_WEEK)
    mock_proc.terminate.assert_called_once()
