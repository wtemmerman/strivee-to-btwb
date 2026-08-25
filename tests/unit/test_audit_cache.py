"""Unit tests for the audit's per-day set cache and its choice of source text."""

from datetime import date

import pytest

from strivee_btwb.core import config as cfg
from strivee_btwb.core.models import INTER, DayProgramming, ProgrammingBlock
from strivee_btwb.pipeline import (
    SETS_SCHEMA_VERSION,
    _parsed_source_mtime_ns,
    _sets_fingerprint,
    do_audit,
    load_sets_day,
    save_day,
    save_formatted_day,
    save_sets_day,
    week_for_audit,
)
from strivee_btwb.processing.volume import METCON, STRENGTH, WorkSet

WEEK = date(2026, 8, 24)


def _day(content: str = "5RM Barbell bench press") -> DayProgramming:
    return DayProgramming(
        date=date(2026, 8, 29),
        day_label="Sat",
        blocks=[ProgrammingBlock(name="EMF 60 : Bench press", content=content)],
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "PARSED_DIR", tmp_path / "parsed")
    monkeypatch.setattr(cfg, "FORMATTED_DIR", tmp_path / "formatted")


# ── The set cache ─────────────────────────────────────────────────────────────


def test_cached_sets_round_trip():
    day = _day()
    sets = [WorkSet("Barbell Bench Press", 1, "5", STRENGTH, source="EMF 60 : Bench press")]
    fingerprint = _sets_fingerprint(day)
    save_sets_day(day, WEEK, fingerprint, sets)
    assert load_sets_day(day, WEEK, fingerprint) == sets


def test_an_empty_extraction_is_cached_rather_than_re_run():
    """A block that genuinely prescribes nothing must not cost an LLM call each time."""
    day = _day()
    fingerprint = _sets_fingerprint(day)
    save_sets_day(day, WEEK, fingerprint, [])
    assert load_sets_day(day, WEEK, fingerprint) == []


def test_a_missing_cache_is_not_an_error():
    assert load_sets_day(_day(), WEEK, "whatever") is None


def test_edited_block_text_invalidates_the_cache():
    day = _day()
    save_sets_day(day, WEEK, _sets_fingerprint(day), [WorkSet("Bench Press", 1, "5", STRENGTH)])
    relevelled = _day("Accumulated 8-10 Reps Banded Bar Muscle-up")
    assert load_sets_day(relevelled, WEEK, _sets_fingerprint(relevelled)) is None


def test_the_fingerprint_covers_the_block_name_as_well_as_its_content():
    """Names carry movements the content never repeats, e.g. 'EMF 60 : Back Squat'."""
    a = DayProgramming(date(2026, 8, 29), "Sat", [ProgrammingBlock("Back Squat", "3 Reps RPE 8")])
    b = DayProgramming(date(2026, 8, 29), "Sat", [ProgrammingBlock("Deadlift", "3 Reps RPE 8")])
    assert _sets_fingerprint(a) != _sets_fingerprint(b)


def test_a_stale_schema_version_invalidates_the_cache():
    """A changed prompt must re-extract, not silently reuse the old reading."""
    day = _day()
    fingerprint = _sets_fingerprint(day)
    path = save_sets_day(day, WEEK, fingerprint, [WorkSet("Bench Press", 1, "5", STRENGTH)])
    stale = path.read_text().replace(
        f'"schema_version": {SETS_SCHEMA_VERSION}', '"schema_version": 0'
    )
    path.write_text(stale)
    assert load_sets_day(day, WEEK, fingerprint) is None


def test_every_field_survives_the_round_trip():
    day = _day()
    sets = [WorkSet("Bar Muscle-up", 6, "2", METCON, source="EMF 60 : Energy System Training")]
    fingerprint = _sets_fingerprint(day)
    save_sets_day(day, WEEK, fingerprint, sets)
    (restored,) = load_sets_day(day, WEEK, fingerprint)
    assert restored.reps == "2"
    assert restored.block_type == METCON
    assert restored.source == "EMF 60 : Energy System Training"


# ── Which text the audit counts ───────────────────────────────────────────────


def test_a_previewed_day_is_counted_at_the_level_that_was_chosen():
    """The athlete trains the selected level, so RX would count a workout they skip."""
    parsed = _day("RX prescription")
    save_day(parsed, WEEK)
    chosen = DayProgramming(
        date=parsed.date,
        day_label="Sat",
        blocks=[ProgrammingBlock("EMF 60 : Bench press", "INTER prescription", level=INTER)],
    )
    save_formatted_day(chosen, WEEK, _parsed_source_mtime_ns(WEEK, "Sat"))
    week, fell_back = week_for_audit(["Sat"], WEEK)
    assert week.days[0].blocks[0].content == "INTER prescription"
    assert fell_back == []


def test_a_day_that_was_never_previewed_falls_back_to_rx_and_says_so():
    save_day(_day("RX prescription"), WEEK)
    week, fell_back = week_for_audit(["Sat"], WEEK)
    assert week.days[0].blocks[0].content == "RX prescription"
    assert fell_back == ["Sat"]


def test_days_with_no_analysis_at_all_are_skipped():
    save_day(_day(), WEEK)
    week, fell_back = week_for_audit(["Mon", "Sat"], WEEK)
    assert [d.day_label for d in week.days] == ["Sat"]


# ── Guard rails on the accessory flags ────────────────────────────────────────


def test_post_without_on_is_refused_before_anything_loads():
    """--post has no date to post to without --on; failing late would open a browser first."""
    with pytest.raises(SystemExit) as exit_info:
        do_audit(["Sat"], WEEK, "gym", on=None, post=True)
    assert exit_info.value.code == 1


def test_a_misspelled_day_is_refused_rather_than_silently_planned():
    with pytest.raises(SystemExit) as exit_info:
        do_audit(["Sat"], WEEK, "gym", on=["Tues"])
    assert exit_info.value.code == 1


def test_a_valid_day_gets_past_the_guards():
    """No analysis cached, so it must fail on that rather than on the day name."""
    with pytest.raises(SystemExit):
        do_audit(["Sat"], WEEK, "gym", on=["Tue"])
