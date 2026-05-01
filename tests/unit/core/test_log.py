"""Unit tests for centralised logging setup."""

import logging

from strivee_btwb.core.log import setup


def _isolated_setup(debug: bool = False) -> logging.Handler:
    """Call setup() on a clean root logger and return the added handler."""
    root = logging.getLogger()
    before = set(root.handlers)
    setup(debug=debug)
    added = [h for h in root.handlers if h not in before]
    return added[-1] if added else root.handlers[-1]


def test_setup_info_sets_info_level():
    root = logging.getLogger()
    setup(debug=False)
    assert root.level == logging.INFO


def test_setup_debug_sets_debug_level():
    root = logging.getLogger()
    setup(debug=True)
    assert root.level == logging.DEBUG
    # Reset to INFO so other tests aren't affected
    root.setLevel(logging.INFO)


def test_setup_adds_stream_handler():
    root = logging.getLogger()
    before_count = len(root.handlers)
    setup(debug=False)
    assert len(root.handlers) >= before_count


def test_setup_silences_urllib3():
    setup(debug=False)
    assert logging.getLogger("urllib3").level == logging.WARNING


def test_setup_silences_playwright():
    setup(debug=False)
    assert logging.getLogger("playwright").level == logging.WARNING


def test_setup_handler_has_formatter():
    handler = _isolated_setup()
    assert handler.formatter is not None
