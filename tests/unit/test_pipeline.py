"""Unit tests for pipeline cache I/O and week-processing functions."""

import json
import logging
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from strivee_btwb.core.models import (
    INTER,
    INTER_PLUS,
    RX,
    DayProgramming,
    ProgrammingBlock,
    WeeklyProgramming,
)
from strivee_btwb.pipeline import (
    CACHE_SCHEMA_VERSION,
    clean_week,
    do_analyse,
    do_capture,
    do_delete,
    do_post,
    do_preview,
    load_days,
    load_formatted_day,
    load_text_captures,
    log_preview,
    log_summary,
    prepare_week_for_btwb,
    save_day,
    save_formatted_day,
    save_text_capture,
    select_levels,
    short_to_date,
    week_start,
)

FIXTURE_WEEK = date(2026, 4, 27)
FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_formatted_dir(tmp_path, monkeypatch):
    """Point the formatted-week cache at a throwaway dir so tests never read or
    write the repo's ./formatted and each test starts cache-cold."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "FORMATTED_DIR", tmp_path / "formatted_cache")


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


def test_save_day_round_trips_scaled_variants(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    day = _make_day(
        blocks=[
            ProgrammingBlock(
                name="WOD", content="20 RMU", inter_plus="10 RMU", inter="20 C2B", instruction="go"
            )
        ]
    )
    save_day(day, FIXTURE_WEEK)

    block = load_days(["Mon"], FIXTURE_WEEK).days[0].blocks[0]
    assert block.inter_plus == "10 RMU"
    assert block.inter == "20 C2B"
    assert block.available_levels() == [RX, INTER_PLUS, INTER]


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


def test_save_day_writes_schema_version(tmp_path, monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    path = save_day(_make_day(), FIXTURE_WEEK)
    assert json.loads(path.read_text())["schema_version"] == CACHE_SCHEMA_VERSION


def test_load_days_warns_on_stale_schema(tmp_path, monkeypatch, caplog):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    # A legacy cache written before versioning existed (no schema_version key).
    (folder / "parsed_2026-04-27_Mon.json").write_text(
        json.dumps({"date": "2026-04-27", "day_label": "Mon", "blocks": []})
    )

    with caplog.at_level(logging.WARNING):
        week = load_days(["Mon"], FIXTURE_WEEK)

    assert len(week.days) == 1  # still loaded, just flagged
    assert "stale" in caplog.text.lower() or "schema_version" in caplog.text


def test_load_days_skips_invalid_cache_without_crashing(tmp_path, monkeypatch, caplog):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    # An older/edited cache with an empty block name (now rejected by the model)
    # must be skipped with a warning, not crash the whole preview/post run.
    (folder / "parsed_2026-04-27_Mon.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "date": "2026-04-27",
                "day_label": "Mon",
                "blocks": [{"name": "", "content": "x"}],
            }
        )
    )
    with caplog.at_level(logging.WARNING):
        week = load_days(["Mon"], FIXTURE_WEEK)
    assert week.days == []  # skipped, not crashed
    assert "skipping unreadable" in caplog.text.lower()


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


def test_clean_week_merge_composes_variants_across_both_halves():
    week = _make_week(
        _make_day(
            blocks=[
                ProgrammingBlock(name="WOD", content="Part A RX", inter="Part A INTER"),
                ProgrammingBlock(name="WOD", content="Part B RX", inter="Part B INTER"),
            ]
        )
    )
    block = clean_week(week).days[0].blocks[0]
    assert block.inter == "Part A INTER\nPart B INTER"


def test_clean_week_merge_falls_back_to_rx_for_unscaled_half():
    """A half with no INTER of its own contributes its RX, so the variant stays whole."""
    week = _make_week(
        _make_day(
            blocks=[
                ProgrammingBlock(name="WOD", content="Warm-up"),
                ProgrammingBlock(name="WOD", content="20 RMU", inter="20 C2B"),
            ]
        )
    )
    block = clean_week(week).days[0].blocks[0]
    assert block.inter == "Warm-up\n20 C2B"


def test_clean_week_merge_leaves_variant_empty_when_neither_half_scales():
    week = _make_week(
        _make_day(
            blocks=[
                ProgrammingBlock(name="WOD", content="Part A"),
                ProgrammingBlock(name="WOD", content="Part B"),
            ]
        )
    )
    block = clean_week(week).days[0].blocks[0]
    assert block.inter == ""
    assert block.inter_plus == ""


def test_select_levels_leaves_single_level_blocks_untouched(monkeypatch):
    def refuse(_prompt):
        raise AssertionError("must not prompt for a block with only one level")

    monkeypatch.setattr("builtins.input", refuse)
    week = _make_week(_make_day(blocks=[ProgrammingBlock(name="WOD", content="21-15-9")]))
    result = select_levels(week)
    assert result.days[0].blocks[0].content == "21-15-9"
    assert result.days[0].blocks[0].level == RX


def test_select_levels_swaps_chosen_variant_into_content(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")
    week = _make_week(
        _make_day(
            blocks=[
                ProgrammingBlock(name="WOD", content="20 RMU", inter_plus="10 RMU", inter="20 C2B")
            ]
        )
    )
    block = select_levels(week).days[0].blocks[0]
    assert block.content == "10 RMU"
    assert block.level == INTER_PLUS


def test_select_levels_menu_skips_absent_levels(monkeypatch):
    """With no INTER+ published, choice 2 is INTER — not an empty INTER+."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")
    week = _make_week(
        _make_day(blocks=[ProgrammingBlock(name="WOD", content="20 RMU", inter="20 C2B")])
    )
    block = select_levels(week).days[0].blocks[0]
    assert block.content == "20 C2B"
    assert block.level == INTER


def test_select_levels_menu_shows_what_differs(monkeypatch, capsys):
    """Levels sharing an opening would otherwise print identical menu lines."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    shared = "For Quality :\n30 reps ring Swing\n+"
    week = _make_week(
        _make_day(
            blocks=[
                ProgrammingBlock(
                    name="WOD",
                    content=f"{shared}\nFor time :\n20/16 RMU",
                    inter=f"{shared}\nAMRAP 6:00\nMax Rep RMU",
                )
            ]
        )
    )
    select_levels(week)
    out = capsys.readouterr().out
    # The shared opening is printed once, in full, above the options — never
    # repeated under each one, and never elided.
    assert "every level starts with:" in out
    assert out.count("30 reps ring Swing") == 1
    assert "For time :" in out
    assert "AMRAP 6:00" in out


def test_select_levels_menu_prints_prescriptions_in_full(monkeypatch, capsys):
    """The menu is the decision surface — a truncated option can't be judged."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    long_inter = "EMOMx9 :\n" + "\n".join(
        f"min {i} - 3 Weighted strict chest to ring" for i in range(1, 8)
    )
    week = _make_week(
        _make_day(
            blocks=[ProgrammingBlock(name="WOD", content="3 sets of :\nMax rep", inter=long_inter)]
        )
    )
    select_levels(week)
    out = capsys.readouterr().out
    for line in long_inter.splitlines():
        assert line in out
    assert "…" not in out


def test_select_levels_bare_enter_keeps_rx(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    week = _make_week(
        _make_day(blocks=[ProgrammingBlock(name="WOD", content="20 RMU", inter="20 C2B")])
    )
    block = select_levels(week).days[0].blocks[0]
    assert block.content == "20 RMU"
    assert block.level == RX


def test_select_levels_reprompts_on_invalid_answer(monkeypatch):
    answers = iter(["9", "abc", "2"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    week = _make_week(
        _make_day(blocks=[ProgrammingBlock(name="WOD", content="20 RMU", inter="20 C2B")])
    )
    assert select_levels(week).days[0].blocks[0].content == "20 C2B"


def test_select_levels_keeps_rx_and_warns_without_a_tty(monkeypatch, caplog):
    def no_input(_prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_input)
    week = _make_week(
        _make_day(blocks=[ProgrammingBlock(name="WOD", content="20 RMU", inter="20 C2B")])
    )
    with caplog.at_level(logging.WARNING):
        block = select_levels(week).days[0].blocks[0]
    assert block.content == "20 RMU"
    assert block.level == RX
    assert any("No input available" in r.message for r in caplog.records)


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


# ── formatted-week cache (prepare_week_for_btwb / save/load_formatted_day) ──────


def test_formatted_cache_roundtrip():
    day = DayProgramming(
        date=FIXTURE_WEEK,
        day_label="Mon",
        blocks=[ProgrammingBlock(name="WOD", content="21-15-9")],
    )
    save_formatted_day(day, FIXTURE_WEEK, source_mtime_ns=12345)
    loaded = load_formatted_day(FIXTURE_WEEK, "Mon", expected_mtime_ns=12345)
    assert loaded is not None
    assert loaded.blocks[0].name == "WOD"
    assert loaded.blocks[0].content == "21-15-9"


def test_formatted_cache_records_selected_level():
    """post reads the level back from the cache, so preview's choice must persist."""
    day = DayProgramming(
        date=FIXTURE_WEEK,
        day_label="Mon",
        blocks=[ProgrammingBlock(name="WOD", content="20 C2B", level=INTER)],
    )
    save_formatted_day(day, FIXTURE_WEEK, source_mtime_ns=1)
    loaded = load_formatted_day(FIXTURE_WEEK, "Mon", expected_mtime_ns=1)
    assert loaded is not None
    assert loaded.blocks[0].level == INTER


def test_formatted_cache_stale_when_source_mtime_changes(caplog):
    day = DayProgramming(
        date=FIXTURE_WEEK, day_label="Tue", blocks=[ProgrammingBlock(name="A", content="x")]
    )
    save_formatted_day(day, FIXTURE_WEEK, source_mtime_ns=100)
    # Parsed source was re-analysed (newer mtime) → cache must be treated as stale.
    assert load_formatted_day(FIXTURE_WEEK, "Tue", expected_mtime_ns=999) is None


def test_formatted_cache_miss_when_absent():
    assert load_formatted_day(FIXTURE_WEEK, "Wed", expected_mtime_ns=1) is None


def test_prepare_week_reuses_cache_on_second_call(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    calls = {"n": 0}

    def counting_format(block, **_):
        calls["n"] += 1
        return block

    monkeypatch.setattr("strivee_btwb.pipeline.format_for_btwb", counting_format)

    first = prepare_week_for_btwb(["Mon", "Tue"], FIXTURE_WEEK)
    assert first.days  # formatted something
    assert calls["n"] > 0
    after_first = calls["n"]

    # Second call must hit the formatted cache and not re-invoke the LLM formatter.
    second = prepare_week_for_btwb(["Mon", "Tue"], FIXTURE_WEEK)
    assert calls["n"] == after_first  # zero additional format calls
    assert [d.day_label for d in second.days] == [d.day_label for d in first.days]


def test_prepare_week_asks_levels_only_for_days_it_formats(monkeypatch):
    """A cached day is neither reformatted nor re-asked about; --relevel forces both."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "PARSED_DIR", FIXTURE_DIR)
    monkeypatch.setattr("strivee_btwb.pipeline.format_for_btwb", lambda block, **_: block)
    asked: list[list[str]] = []

    def record(week):
        asked.append([d.day_label for d in week.days])
        return week

    monkeypatch.setattr("strivee_btwb.pipeline.select_levels", record)

    prepare_week_for_btwb(["Mon", "Tue"], FIXTURE_WEEK)
    assert asked == [["Mon", "Tue"]]

    prepare_week_for_btwb(["Mon", "Tue"], FIXTURE_WEEK)
    assert len(asked) == 1  # cache hit: no second round of questions

    prepare_week_for_btwb(["Mon", "Tue"], FIXTURE_WEEK, relevel=True)
    assert asked == [["Mon", "Tue"], ["Mon", "Tue"]]


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


# ── do_delete ─────────────────────────────────────────────────────────────────


def test_do_delete_passes_iso_dates_for_requested_days(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_EMAIL", "test@example.com")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "password")

    mock_delete = MagicMock(return_value=[{"id": "1", "ok": True}])
    monkeypatch.setattr("strivee_btwb.pipeline.delete_week", mock_delete)

    do_delete(["Mon", "Tue"], yes=True, headless=True, ws=FIXTURE_WEEK)

    kwargs = mock_delete.call_args.kwargs
    assert kwargs["dates"] == ["2026-04-27", "2026-04-28"]
    assert kwargs["confirm"] is None  # --yes skips confirmation


def test_do_delete_uses_confirm_prompt_when_not_yes(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_EMAIL", "x")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "x")
    mock_delete = MagicMock(return_value=[])
    monkeypatch.setattr("strivee_btwb.pipeline.delete_week", mock_delete)

    do_delete(["Mon"], yes=False, headless=True, ws=FIXTURE_WEEK)
    assert callable(mock_delete.call_args.kwargs["confirm"])


def test_do_delete_dry_run_skips_confirm(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_EMAIL", "x")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "x")
    mock_delete = MagicMock(return_value=[{"id": "1", "dry_run": True}])
    monkeypatch.setattr("strivee_btwb.pipeline.delete_week", mock_delete)

    do_delete(["Mon"], yes=False, headless=True, ws=FIXTURE_WEEK, dry_run=True)
    assert mock_delete.call_args.kwargs["dry_run"] is True
    assert mock_delete.call_args.kwargs["confirm"] is None  # dry-run never prompts


def test_do_delete_exits_without_credentials(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_EMAIL", "")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "")
    with pytest.raises(SystemExit):
        do_delete(["Mon"], yes=True, headless=True, ws=FIXTURE_WEEK)


def test_do_delete_exits_on_auth_error(monkeypatch):
    import strivee_btwb.core.config as cfg
    from strivee_btwb.btwb import AuthenticationError

    monkeypatch.setattr(cfg, "BTWB_EMAIL", "x")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "x")
    monkeypatch.setattr(
        "strivee_btwb.pipeline.delete_week",
        MagicMock(side_effect=AuthenticationError("bad creds")),
    )
    with pytest.raises(SystemExit):
        do_delete(["Mon"], yes=True, headless=True, ws=FIXTURE_WEEK)


def test_do_delete_exits_on_generic_exception(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "BTWB_EMAIL", "x")
    monkeypatch.setattr(cfg, "BTWB_PASSWORD", "x")
    monkeypatch.setattr(
        "strivee_btwb.pipeline.delete_week", MagicMock(side_effect=RuntimeError("browser crashed"))
    )
    with pytest.raises(SystemExit):
        do_delete(["Mon"], yes=True, headless=True, ws=FIXTURE_WEEK)


def test_confirm_delete_accepts_y(monkeypatch, capsys):
    from strivee_btwb.pipeline import _confirm_delete

    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert _confirm_delete([{"date": "2026-06-08", "title": "WOD"}]) is True
    assert "permanently delete" in capsys.readouterr().out.lower()


def test_confirm_delete_defaults_to_no(monkeypatch):
    from strivee_btwb.pipeline import _confirm_delete

    monkeypatch.setattr("builtins.input", lambda _: "")  # bare Enter
    assert _confirm_delete([{"date": "2026-06-08", "title": "WOD"}]) is False


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


def test_do_analyse_aborts_when_all_days_fail(monkeypatch, tmp_path, caplog):
    """A per-day exception is caught and logged; when EVERY day fails it aborts."""
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
        with pytest.raises(SystemExit):
            do_analyse(["Mon"], ws=FIXTURE_WEEK)
    # The per-day ValueError was caught/logged (not raised), then the run aborted
    # rather than reporting success with zero cached days.
    assert "analysis failed" in caplog.text.lower()


def test_do_analyse_continues_on_partial_failure(monkeypatch, tmp_path, caplog):
    """One failing day is logged but does not abort when another day succeeds."""
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURES_DIR", tmp_path)
    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path)
    folder = tmp_path / FIXTURE_WEEK.isoformat()
    folder.mkdir()
    (folder / "strivee_20260427_120000_Mon.txt").write_text("mon text", encoding="utf-8")
    (folder / "strivee_20260428_120000_Tue.txt").write_text("tue text", encoding="utf-8")

    def fake_extract(*, day_label, **_):
        if day_label == "Mon":
            raise ValueError("model error")
        return _make_day(label="Tue", day_date=date(2026, 4, 28))

    monkeypatch.setattr("strivee_btwb.pipeline.extract_day_programming_from_text", fake_extract)
    with caplog.at_level(logging.ERROR):
        do_analyse(["Mon", "Tue"], ws=FIXTURE_WEEK)  # no SystemExit
    assert "analysis failed" in caplog.text.lower()
    assert any(folder.glob("parsed_*_Tue.json"))


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
