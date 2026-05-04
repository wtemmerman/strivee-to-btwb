"""Unit tests for capture helpers (no ADB required)."""

import io
import subprocess
from datetime import date
from unittest.mock import MagicMock, patch

from PIL import Image

from strivee_btwb.capture import save_capture, stitch_vertical
from strivee_btwb.capture.adb import (
    _adb,
    _change_fraction,
    _crop_frame,
    _device_size,
    _find_element_center,
    _screens_same,
    _tap,
    _ui_dump,
    capture_day_screenshots,
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
# stitch_vertical
# ---------------------------------------------------------------------------


def test_stitch_vertical_single_image():
    img = _solid((100, 100, 100), width=80, height=120)
    result = stitch_vertical([img])
    assert result is img


def test_stitch_vertical_multiple_images():
    # Plain concat: 3 frames of height 50 → total 150
    imgs = [_solid((i * 50, 0, 0), width=100, height=50) for i in range(3)]
    result = stitch_vertical(imgs)
    assert result.width == 100
    assert result.height == 150


def test_stitch_vertical_different_widths():
    imgs = [_solid((0, 0, 0), width=w, height=50) for w in (80, 100, 60)]
    result = stitch_vertical(imgs)
    assert result.width == 100  # max width


# ---------------------------------------------------------------------------
# save_capture
# ---------------------------------------------------------------------------


def test_save_capture_creates_file(tmp_path):
    img = _solid((255, 0, 0), width=50, height=50)
    path = save_capture(img, label="Mon", output_dir=tmp_path)
    assert path.exists()
    assert path.suffix == ".png"
    assert "Mon" in path.name


def test_save_capture_per_week_folder(tmp_path):
    img = _solid((0, 255, 0), width=50, height=50)
    ws = date(2026, 4, 27)
    path = save_capture(img, label="Tue", week_start=ws, output_dir=tmp_path)
    assert path.parent.name == "2026-04-27"
    assert path.exists()


def test_save_capture_no_label(tmp_path):
    img = _solid((0, 0, 255), width=50, height=50)
    path = save_capture(img, output_dir=tmp_path)
    assert path.exists()
    assert path.name.startswith("strivee_")


# ---------------------------------------------------------------------------
# _crop_frame
# ---------------------------------------------------------------------------


def test_crop_frame_no_crop_returns_original(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURE_CROP_TOP", 0)
    monkeypatch.setattr(cfg, "CAPTURE_CROP_BOTTOM", 0)
    img = _solid((100, 100, 100), width=100, height=200)
    result = _crop_frame(img)
    assert result is img


def test_crop_frame_crops_top(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURE_CROP_TOP", 50)
    monkeypatch.setattr(cfg, "CAPTURE_CROP_BOTTOM", 0)
    img = _solid((100, 100, 100), width=100, height=200)
    result = _crop_frame(img)
    assert result.height == 150


def test_crop_frame_crops_bottom(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURE_CROP_TOP", 0)
    monkeypatch.setattr(cfg, "CAPTURE_CROP_BOTTOM", 30)
    img = _solid((100, 100, 100), width=100, height=200)
    result = _crop_frame(img)
    assert result.height == 170


def test_crop_frame_crops_top_and_bottom(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURE_CROP_TOP", 40)
    monkeypatch.setattr(cfg, "CAPTURE_CROP_BOTTOM", 30)
    img = _solid((100, 100, 100), width=100, height=200)
    result = _crop_frame(img)
    assert result.height == 130


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
    with patch("strivee_btwb.capture.adb._adb", return_value=_fake_proc(b"Physical size: 1080x2400")):
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
    monkeypatch.setattr("strivee_btwb.capture.adb.find_strivee_package", lambda *_: "com.strivee.app")
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

    monkeypatch.setattr(cfg, "CAPTURE_CROP_TOP", 550)
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))
    tapped = []
    monkeypatch.setattr("strivee_btwb.capture.adb._tap", lambda x, y, *_, **__: tapped.append((x, y)))
    assert navigate_to_day("Mon") is True
    # Mon is index 0 → x = int((0 + 0.5) * 1080 / 7) = 77, y = 550 - 50 = 500
    assert tapped == [(77, 500)]


def test_navigate_to_day_tries_french_fallback(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURE_CROP_TOP", 0)  # force fallback path
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))
    no_match_xml = """<?xml version="1.0"?><hierarchy><node text="Lun" bounds="[0,100][270,200]"/></hierarchy>"""
    monkeypatch.setattr("strivee_btwb.capture.adb._ui_dump", lambda *_: no_match_xml)
    monkeypatch.setattr("strivee_btwb.capture.adb._tap", lambda *_, **__: None)
    assert navigate_to_day("Mon") is True  # "Mon" → "Lun" fallback


def test_navigate_to_day_returns_false_when_not_found(monkeypatch):
    assert navigate_to_day("Invalid") is False


def test_navigate_to_day_returns_false_when_ui_fallback_finds_nothing(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURE_CROP_TOP", 0)  # force fallback path
    monkeypatch.setattr("strivee_btwb.capture.adb._device_size", lambda *_: (1080, 2400))
    empty_xml = """<?xml version="1.0"?><hierarchy><node text="Other" bounds="[0,0][100,100]"/></hierarchy>"""
    monkeypatch.setattr("strivee_btwb.capture.adb._ui_dump", lambda *_: empty_xml)
    assert navigate_to_day("Sun") is False


def test_capture_day_screenshots_returns_frames(monkeypatch):
    import strivee_btwb.core.config as cfg

    monkeypatch.setattr(cfg, "CAPTURE_CROP_TOP", 0)
    monkeypatch.setattr(cfg, "CAPTURE_CROP_BOTTOM", 0)
    white = _solid((255, 255, 255), width=100, height=200)
    black = _solid((0, 0, 0), width=100, height=200)
    # navigate, then 2 shots: one different (scroll succeeded), then identical (bottom)
    screenshots = iter([white, black, black])
    monkeypatch.setattr("strivee_btwb.capture.adb.navigate_to_day", lambda *_, **__: True)
    monkeypatch.setattr("strivee_btwb.capture.adb.take_screenshot", lambda *_: next(screenshots))
    monkeypatch.setattr("strivee_btwb.capture.adb.swipe_up", lambda *_, **__: None)
    monkeypatch.setattr("strivee_btwb.capture.adb.time.sleep", lambda _: None)
    frames = capture_day_screenshots("Mon", max_scrolls=3)
    assert len(frames) >= 1
