"""Unit tests for capture helpers (no ADB required)."""

import io
import subprocess
from datetime import date
from unittest.mock import patch

from PIL import Image

from strivee_btwb.capture.adb import (
    _adb,
    _change_fraction,
    _device_size,
    _find_element_center,
    _screens_same,
    _tap,
    _texts_from_dump,
    _ui_dump,
    capture_day_as_text,
    find_strivee_package,
    launch_strivee,
    navigate_to_day,
    scroll_to_top,
    swipe_down,
    swipe_up,
    take_screenshot,
)

# ---------------------------------------------------------------------------
# _change_fraction / _screens_same
# ---------------------------------------------------------------------------


def _solid(color: tuple[int, int, int], width: int = 100, height: int = 200) -> Image.Image:
    return Image.new("RGB", (width, height), color)


def test_change_fraction_identical_images():
    img = _solid((255, 255, 255))
    assert _change_fraction(img, img) == 0.0


def test_change_fraction_completely_different():
    white = _solid((255, 255, 255))
    black = _solid((0, 0, 0))
    frac = _change_fraction(white, black)
    assert frac > 0.5


def test_screens_same_identical():
    img = _solid((200, 200, 200))
    assert _screens_same(img, img)


def test_screens_same_different():
    white = _solid((255, 255, 255))
    black = _solid((0, 0, 0))
    assert not _screens_same(white, black)


# ---------------------------------------------------------------------------
# _find_element_center
# ---------------------------------------------------------------------------


_UI_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node bounds="[0,0][1080,2400]">
    <node text="Mon" bounds="[0,100][270,200]" />
    <node text="Tue" bounds="[270,100][540,200]" />
    <node content-desc="Settings" bounds="[900,50][1080,150]" />
  </node>
</hierarchy>"""


def test_find_element_center_by_text():
    center = _find_element_center(_UI_XML, "Mon")
    assert center == (135, 150)


def test_find_element_center_case_insensitive():
    center = _find_element_center(_UI_XML, "mon")
    assert center is not None


def test_find_element_center_by_content_desc():
    center = _find_element_center(_UI_XML, "Settings")
    assert center == (990, 100)


def test_find_element_center_not_found():
    assert _find_element_center(_UI_XML, "Sun") is None


def test_find_element_center_invalid_xml():
    assert _find_element_center("not xml at all", "Mon") is None


# ---------------------------------------------------------------------------
# subprocess-mocked ADB functions
# ---------------------------------------------------------------------------


def _fake_proc(stdout: bytes = b"", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def test_adb_calls_subprocess_run():
    with patch("subprocess.run", return_value=_fake_proc()) as mock_run:
        result = _adb(["shell", "echo", "hi"])
        assert mock_run.called
        assert result.returncode == 0


def test_adb_with_serial_prepends_flag():
    with patch("subprocess.run", return_value=_fake_proc()) as mock_run:
        _adb(["shell", "echo"], serial="emulator-5554")
        cmd = mock_run.call_args[0][0]
        assert "-s" in cmd
        assert "emulator-5554" in cmd


def test_device_size_parses_output():
    with patch(
        "strivee_btwb.capture.adb._adb", return_value=_fake_proc(b"Physical size: 1080x2400")
    ):
        w, h = _device_size()
        assert w == 1080
        assert h == 2400


def test_device_size_returns_default_on_bad_output():
    with patch("strivee_btwb.capture.adb._adb", return_value=_fake_proc(b"no match here")):
        w, h = _device_size()
        assert (w, h) == (1080, 2400)


def test_find_strivee_package_returns_package():
    packages = b"package:com.strivee.app\npackage:com.other"
    with patch("strivee_btwb.capture.adb._adb", return_value=_fake_proc(packages)):
        pkg = find_strivee_package()
        assert pkg == "com.strivee.app"


def test_find_strivee_package_raises_when_not_found():
    import pytest

    with patch("strivee_btwb.capture.adb._adb", return_value=_fake_proc(b"package:com.other")):
        with pytest.raises(RuntimeError, match="Strivee not found"):
            find_strivee_package()


def test_launch_strivee_calls_adb(monkeypatch):
    monkeypatch.setattr(
        "strivee_btwb.capture.adb.find_strivee_package", lambda *_: "com.strivee.app"
    )
    monkeypatch.setattr("strivee_btwb.capture.adb.time.sleep", lambda _: None)
    with patch("strivee_btwb.capture.adb._adb", return_value=_fake_proc()) as mock_adb:
        launch_strivee()
        mock_adb.assert_called_once()


def test_take_screenshot_returns_image():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (255, 0, 0)).save(buf, format="PNG")
    png_bytes = buf.getvalue()
    with patch("strivee_btwb.capture.adb._adb", return_value=_fake_proc(png_bytes)):
        img = take_screenshot()
        assert img.mode == "RGB"
        assert img.size == (10, 10)


def test_take_screenshot_raises_on_empty_response():
    import pytest

    with patch("strivee_btwb.capture.adb._adb", return_value=_fake_proc(b"")):
        with pytest.raises(RuntimeError, match="no data"):
            take_screenshot()


def test_swipe_up_calls_adb(monkeypatch):
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))
    monkeypatch.setattr("strivee_btwb.capture.adb.time.sleep", lambda _: None)
    with patch("strivee_btwb.capture.adb._adb", return_value=_fake_proc()) as mock_adb:
        swipe_up()
        mock_adb.assert_called_once()
        cmd = mock_adb.call_args[0][0]
        assert "swipe" in cmd


def test_swipe_down_calls_adb(monkeypatch):
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))
    monkeypatch.setattr("strivee_btwb.capture.adb.time.sleep", lambda _: None)
    with patch("strivee_btwb.capture.adb._adb", return_value=_fake_proc()) as mock_adb:
        swipe_down()
        mock_adb.assert_called_once()
        cmd = mock_adb.call_args[0][0]
        assert "swipe" in cmd


def test_scroll_to_top_stops_when_screen_unchanged(monkeypatch):
    img = _solid((200, 200, 200))
    monkeypatch.setattr("strivee_btwb.capture.adb.take_screenshot", lambda *_: img)
    monkeypatch.setattr("strivee_btwb.capture.adb.swipe_down", lambda *_, **__: None)
    scroll_to_top()  # should terminate after first identical pair


def test_ui_dump_returns_string():
    with patch("strivee_btwb.capture.adb._adb", return_value=_fake_proc(b"<hierarchy/>")):
        result = _ui_dump()
        assert isinstance(result, str)


def test_tap_calls_adb(monkeypatch):
    monkeypatch.setattr("strivee_btwb.capture.adb.time.sleep", lambda _: None)
    with patch("strivee_btwb.capture.adb._adb", return_value=_fake_proc()) as mock_adb:
        _tap(540, 1200)
        mock_adb.assert_called_once()


def test_navigate_to_day_returns_true_when_found(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "DAY_TAB_Y", 500)
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))
    tapped = []
    monkeypatch.setattr(
        "strivee_btwb.capture.adb._tap", lambda x, y, *_, **__: tapped.append((x, y))
    )
    assert navigate_to_day("Mon") is True
    # Mon is index 0 → x = int((0 + 0.5) * 1080 / 7) = 77, y = DAY_TAB_Y = 500
    assert tapped == [(77, 500)]


def test_navigate_to_day_tries_french_fallback(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "DAY_TAB_Y", 0)  # force UI-detection fallback
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))
    no_match_xml = (
        '<?xml version="1.0"?><hierarchy><node text="Lun" bounds="[0,100][270,200]"/></hierarchy>'
    )
    monkeypatch.setattr("strivee_btwb.capture.adb._ui_dump", lambda *_: no_match_xml)
    monkeypatch.setattr("strivee_btwb.capture.adb._tap", lambda *_, **__: None)
    assert navigate_to_day("Mon") is True  # "Mon" → "Lun" fallback


def test_navigate_to_day_returns_false_when_not_found(monkeypatch):
    assert navigate_to_day("Invalid") is False


def test_navigate_to_day_returns_false_when_ui_fallback_finds_nothing(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "DAY_TAB_Y", 0)  # force UI-detection fallback
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))
    empty_xml = (
        '<?xml version="1.0"?><hierarchy><node text="Other" bounds="[0,0][100,100]"/></hierarchy>'
    )
    monkeypatch.setattr("strivee_btwb.capture.adb._ui_dump", lambda *_: empty_xml)
    assert navigate_to_day("Sun") is False


# ---------------------------------------------------------------------------
# _texts_from_dump
# ---------------------------------------------------------------------------

_DUMP_XML = """<?xml version="1.0"?>
<hierarchy>
  <node text="EMF 60 : Snatch" bounds="[0,0][1080,100]" />
  <node text="" bounds="[0,100][1080,200]" />
  <node text="Build to 1RM" bounds="[0,200][1080,300]" />
  <node text="   " bounds="[0,300][1080,400]" />
</hierarchy>"""


def test_texts_from_dump_returns_non_empty_texts():
    result = _texts_from_dump(_DUMP_XML)
    assert "EMF 60 : Snatch" in result
    assert "Build to 1RM" in result


def test_texts_from_dump_ignores_empty_and_whitespace():
    result = _texts_from_dump(_DUMP_XML)
    assert "" not in result
    assert "   " not in result


def test_texts_from_dump_invalid_xml_returns_empty():
    assert _texts_from_dump("not xml at all") == []


def test_texts_from_dump_empty_hierarchy():
    assert _texts_from_dump("<hierarchy/>") == []


# ---------------------------------------------------------------------------
# capture_day_as_text
# ---------------------------------------------------------------------------


def test_capture_day_as_text_returns_string(monkeypatch):
    monkeypatch.setattr("strivee_btwb.capture.adb.navigate_to_day", lambda *_, **__: True)
    monkeypatch.setattr("strivee_btwb.capture.adb.time.sleep", lambda _: None)

    white = _solid((255, 255, 255))
    call_count = {"n": 0}

    def fake_screenshot(*_):
        call_count["n"] += 1
        return white

    monkeypatch.setattr("strivee_btwb.capture.adb.take_screenshot", fake_screenshot)
    monkeypatch.setattr("strivee_btwb.capture.adb.swipe_up", lambda *_, **__: None)
    monkeypatch.setattr("strivee_btwb.capture.adb.scroll_to_top", lambda *_: None)
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))
    monkeypatch.setattr(
        "strivee_btwb.capture.adb._ui_dump",
        lambda *_: '<hierarchy><node text="EMF 60 : Snatch" bounds="[0,0][100,100]"/></hierarchy>',
    )
    result = capture_day_as_text("Mon", max_scrolls=1)
    assert isinstance(result, str)
    assert "EMF 60 : Snatch" in result


def test_navigate_to_week_same_week_returns_early(monkeypatch):
    """navigate_to_week does nothing when target is the current week."""
    from datetime import timedelta

    from strivee_btwb.capture.adb import navigate_to_week

    today = date.today()
    current_week = today - timedelta(days=today.weekday())
    adb_calls = []
    monkeypatch.setattr("strivee_btwb.capture.adb._adb", lambda *a, **k: adb_calls.append(a))
    navigate_to_week(current_week)
    assert adb_calls == []


def test_navigate_to_week_future_week_swipes_left(monkeypatch):
    """navigate_to_week swipes left to reach a future week."""
    from datetime import timedelta

    import strivee_btwb.core.config as cfg
    from strivee_btwb.capture.adb import navigate_to_week

    today = date.today()
    next_week = today - timedelta(days=today.weekday()) + timedelta(weeks=1)

    monkeypatch.setattr(cfg, "DAY_TAB_Y", 0)
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))
    monkeypatch.setattr("strivee_btwb.capture.adb.time.sleep", lambda _: None)

    adb_calls = []
    monkeypatch.setattr(
        "strivee_btwb.capture.adb._adb", lambda *a, **k: adb_calls.append(a) or _fake_proc()
    )
    navigate_to_week(next_week)
    assert len(adb_calls) == 1


def test_navigate_to_week_past_week_swipes_right(monkeypatch):
    """navigate_to_week swipes right to reach a past week."""
    from datetime import timedelta

    import strivee_btwb.core.config as cfg
    from strivee_btwb.capture.adb import navigate_to_week

    today = date.today()
    last_week = today - timedelta(days=today.weekday()) - timedelta(weeks=1)

    monkeypatch.setattr(cfg, "DAY_TAB_Y", 0)
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))
    monkeypatch.setattr("strivee_btwb.capture.adb.time.sleep", lambda _: None)

    adb_calls = []
    monkeypatch.setattr(
        "strivee_btwb.capture.adb._adb", lambda *a, **k: adb_calls.append(a) or _fake_proc()
    )
    navigate_to_week(last_week)
    assert len(adb_calls) == 1


def test_capture_day_as_text_deduplicates_lines(monkeypatch):
    monkeypatch.setattr("strivee_btwb.capture.adb.navigate_to_day", lambda *_, **__: True)
    monkeypatch.setattr("strivee_btwb.capture.adb.time.sleep", lambda _: None)
    monkeypatch.setattr("strivee_btwb.capture.adb.scroll_to_top", lambda *_: None)
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))

    white = _solid((255, 255, 255))
    # Always same screen → _screens_same returns True → stops after first iteration
    monkeypatch.setattr("strivee_btwb.capture.adb.take_screenshot", lambda *_: white)
    monkeypatch.setattr("strivee_btwb.capture.adb.swipe_up", lambda *_, **__: None)
    # Dump returns same text each call
    monkeypatch.setattr(
        "strivee_btwb.capture.adb._ui_dump",
        lambda *_: '<hierarchy><node text="EMF 60 : Snatch" bounds="[0,0][100,100]"/></hierarchy>',
    )
    result = capture_day_as_text("Mon", max_scrolls=3)
    # "EMF 60 : Snatch" should appear only once despite multiple dumps
    assert result.count("EMF 60 : Snatch") == 1
