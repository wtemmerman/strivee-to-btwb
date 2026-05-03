"""Unit tests for pipeline cache I/O and week-processing functions."""

import json
import logging
import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from strivee_btwb.core.models import DayProgramming, ProgrammingBlock, WeeklyProgramming
from strivee_btwb.pipeline import (
    clean_week,
    do_analyse,
    do_capture,
    do_post,
    do_preview,
    load_captures,
    load_days,
    log_preview,
    log_summary,
    save_day,
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


def _solid_png(path: Path, width: int = 10, height: int = 10) -> Path:
    Image.new("RGB", (width, height), (200, 200, 200)).save(path)
    return path


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


# ── load_captures ─────────────────────────────────────────────────────────────


def test_load_captures_reads_png(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    _solid_png(folder / "strivee_20260427_120000_Mon.png")

    result = load_captures(["Mon"], FIXTURE_WEEK)

    assert "Mon" in result
    assert len(result["Mon"]) == 1
    assert isinstance(result["Mon"][0], Image.Image)


def test_load_captures_picks_latest_when_multiple(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    _solid_png(folder / "strivee_20260427_110000_Mon.png")
    _solid_png(folder / "strivee_20260427_120000_Mon.png")

    result = load_captures(["Mon"], FIXTURE_WEEK)
    assert len(result["Mon"]) == 1


def test_load_captures_warns_on_missing(tmp_path, monkeypatch, caplog):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    (tmp_path / FIXTURE_WEEK.isoformat()).mkdir()

    with caplog.at_level(logging.WARNING):
        result = load_captures(["Mon"], FIXTURE_WEEK)

    assert result == {}
    assert "Mon" in caplog.text


# ── clean_week ────────────────────────────────────────────────────────────────


def test_clean_week_removes_empty_blocks():
    week = _make_week(
        _make_day(blocks=[
            ProgrammingBlock(name="Back Squat", content="5x5"),
            ProgrammingBlock(name="Empty", content="   "),
        ])
    )
    result = clean_week(week)
    assert len(result.days[0].blocks) == 1
    assert result.days[0].blocks[0].name == "Back Squat"


def test_clean_week_merges_consecutive_same_name():
    week = _make_week(
        _make_day(blocks=[
            ProgrammingBlock(name="WOD", content="Part A"),
            ProgrammingBlock(name="WOD", content="Part B"),
        ])
    )
    result = clean_week(week)
    assert len(result.days[0].blocks) == 1
    assert "Part A" in result.days[0].blocks[0].content
    assert "Part B" in result.days[0].blocks[0].content


def test_clean_week_does_not_merge_different_names():
    week = _make_week(
        _make_day(blocks=[
            ProgrammingBlock(name="Strength", content="5x5"),
            ProgrammingBlock(name="WOD", content="21-15-9"),
        ])
    )
    result = clean_week(week)
    assert len(result.days[0].blocks) == 2


def test_clean_week_drops_day_when_all_blocks_empty():
    week = _make_week(
        _make_day(blocks=[ProgrammingBlock(name="Rest", content="  ")])
    )
    result = clean_week(week)
    assert result.days == []


def test_clean_week_merge_is_case_insensitive():
    week = _make_week(
        _make_day(blocks=[
            ProgrammingBlock(name="wod", content="Part A"),
            ProgrammingBlock(name="WOD", content="Part B"),
        ])
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
    do_preview(["Mon", "Tue"])


def test_do_preview_exits_when_no_cache(monkeypatch, tmp_path):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    (tmp_path / FIXTURE_WEEK.isoformat()).mkdir()
    with pytest.raises(SystemExit):
        do_preview(["Mon"])


# ── do_analyse ────────────────────────────────────────────────────────────────


def test_do_analyse_success(monkeypatch, tmp_path):
    import strivee_btwb.core.config as cfg
    from PIL import Image

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    _solid_png(folder / "strivee_20260427_120000_Mon.png")

    fake_day = _make_day()
    monkeypatch.setattr(
        "strivee_btwb.pipeline.extract_day_programming",
        lambda **_: fake_day,
    )
    do_analyse(["Mon"])
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
    _solid_png(folder / "strivee_20260427_120000_Mon.png")

    empty_day = DayProgramming(date=date(2026, 4, 27), day_label="Mon", blocks=[])
    monkeypatch.setattr(
        "strivee_btwb.pipeline.extract_day_programming",
        lambda **_: empty_day,
    )
    with caplog.at_level(logging.WARNING):
        do_analyse(["Mon"])
    assert "no blocks" in caplog.text.lower()


# ── do_capture ────────────────────────────────────────────────────────────────


def test_do_capture_success(monkeypatch, tmp_path):
    import strivee_btwb.core.config as cfg
    from PIL import Image

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(cfg, "ANDROID_SERIAL", None)
    monkeypatch.setattr(cfg, "MAX_SCROLLS", 2)
    monkeypatch.setattr("strivee_btwb.pipeline.launch_scrcpy", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.launch_strivee", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.navigate_to_week", MagicMock())
    monkeypatch.setattr("strivee_btwb.pipeline.scroll_to_top", MagicMock())

    dummy = Image.new("RGB", (10, 10))
    monkeypatch.setattr(
        "strivee_btwb.pipeline.capture_day_screenshots", lambda *a, **k: [dummy]
    )
    monkeypatch.setattr("strivee_btwb.pipeline.stitch_vertical", lambda imgs: imgs[0])
    saved_path = tmp_path / "strivee_Mon.png"
    monkeypatch.setattr("strivee_btwb.pipeline.save_capture", lambda *a, **k: saved_path)

    do_capture(["Mon"], no_scrcpy=True)


def test_do_capture_exits_when_strivee_fails(monkeypatch, tmp_path):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "ANDROID_SERIAL", None)
    monkeypatch.setattr("strivee_btwb.pipeline.launch_strivee", MagicMock(side_effect=RuntimeError("no device")))
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
        "strivee_btwb.pipeline.capture_day_screenshots",
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

    do_post(["Mon"], yes=True, headless=True)
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
