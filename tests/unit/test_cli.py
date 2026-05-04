"""Unit tests for CLI argument parsing."""

import sys
from unittest.mock import patch

import pytest

from strivee_btwb.cli import _build_parser, main
from strivee_btwb.pipeline import parse_days

# ── parse_days ────────────────────────────────────────────────────────────────


def test_parse_days_none_returns_mon_to_sat():
    result = parse_days(None)
    assert result == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]


def test_parse_days_single():
    assert parse_days("Mon") == ["Mon"]


def test_parse_days_comma_separated():
    assert parse_days("Mon,Wed,Fri") == ["Mon", "Wed", "Fri"]


def test_parse_days_strips_whitespace():
    assert parse_days("Mon, Tue , Wed") == ["Mon", "Tue", "Wed"]


# ── _build_parser ─────────────────────────────────────────────────────────────


def test_parser_has_all_subcommands():
    parser = _build_parser()
    # Parse each command — no error means the subcommand exists
    for cmd in ("capture", "analyse", "preview", "post", "run"):
        args = parser.parse_args([cmd])
        assert args.command == cmd


def test_parser_capture_no_scrcpy_flag():
    args = _build_parser().parse_args(["capture", "--no-scrcpy"])
    assert args.no_scrcpy is True


def test_parser_post_yes_flag():
    args = _build_parser().parse_args(["post", "--yes"])
    assert args.yes is True


def test_parser_post_headless_flag():
    args = _build_parser().parse_args(["post", "--headless"])
    assert args.headless is True


def test_parser_post_days_flag():
    args = _build_parser().parse_args(["post", "--days", "Mon,Tue"])
    assert args.days == "Mon,Tue"


def test_parser_debug_flag():
    args = _build_parser().parse_args(["--debug", "capture"])
    assert args.debug is True


def test_parser_requires_subcommand():
    with pytest.raises(SystemExit):
        _build_parser().parse_args([])


# ── main() dispatch ───────────────────────────────────────────────────────────


def _run_main(argv: list[str]) -> None:
    with (
        patch.object(sys, "argv", ["strivee-btwb", *argv]),
        patch("strivee_btwb.cli.log") as mock_log,
    ):
        mock_log.setup.return_value = None
        main()


def test_main_capture_calls_do_capture(monkeypatch):
    with patch("strivee_btwb.cli.do_capture") as mock:
        _run_main(["capture"])
        mock.assert_called_once()


def test_main_analyse_calls_do_analyse(monkeypatch):
    with patch("strivee_btwb.cli.do_analyse") as mock:
        _run_main(["analyse"])
        mock.assert_called_once()


def test_main_preview_calls_do_preview(monkeypatch):
    with patch("strivee_btwb.cli.do_preview") as mock:
        _run_main(["preview"])
        mock.assert_called_once()


def test_main_post_calls_do_post(monkeypatch):
    with patch("strivee_btwb.cli.do_post") as mock:
        _run_main(["post", "--yes"])
        mock.assert_called_once()
        args = mock.call_args[0]
        assert args[1] is True  # yes=True


def test_main_run_calls_all_steps(monkeypatch):
    with (
        patch("strivee_btwb.cli.do_capture") as mc,
        patch("strivee_btwb.cli.do_analyse") as ma,
        patch("strivee_btwb.cli.do_preview") as mp,
        patch("strivee_btwb.cli.do_post") as mpost,
    ):
        _run_main(["run", "--yes"])
        mc.assert_called_once()
        ma.assert_called_once()
        mp.assert_called_once()
        mpost.assert_called_once()


def test_main_capture_no_scrcpy_flag(monkeypatch):
    with patch("strivee_btwb.cli.do_capture") as mock:
        _run_main(["capture", "--no-scrcpy"])
        args = mock.call_args[0]
        assert args[1] is True  # no_scrcpy=True


def test_main_post_headless_flag(monkeypatch):
    with patch("strivee_btwb.cli.do_post") as mock:
        _run_main(["post", "--headless"])
        args = mock.call_args[0]
        assert args[2] is True  # headless=True


def test_main_days_flag_passed_through(monkeypatch):
    with patch("strivee_btwb.cli.do_capture") as mock:
        _run_main(["capture", "--days", "Mon,Tue"])
        args = mock.call_args[0]
        assert args[0] == ["Mon", "Tue"]
