"""
Android device capture via ADB.

Primary flow per day:
  1. launch_scrcpy()     — open scrcpy for visual feedback (optional)
  2. launch_strivee()    — open the Strivee app on the device
  3. scroll_to_top()     — reset scroll position
  4. navigate_to_week()  — swipe to the target week
  5. capture_day_as_text() — tap day tab, scroll down, collect UI text dump

ADB must be on PATH and USB debugging enabled on the device.
"""

import io
import logging
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from PIL import Image, ImageChops

from ..core import config

logger = logging.getLogger("capture")

_WEEKDAYS_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WEEKDAYS_FR = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
_FR_MAP = dict(zip(_WEEKDAYS_EN, _WEEKDAYS_FR))


# ---------------------------------------------------------------------------
# ADB helpers
# ---------------------------------------------------------------------------


def _adb(
    cmd: list[str], serial: str | None = None, timeout: int = 15
) -> subprocess.CompletedProcess[bytes]:
    """Run an adb command and return the completed process."""
    prefix = ["adb"] + (["-s", serial] if serial else [])
    return subprocess.run(prefix + cmd, capture_output=True, timeout=timeout, check=False)


def _device_size(serial: str | None = None) -> tuple[int, int]:
    """Return (width, height) of the connected device screen in pixels."""
    result = _adb(["shell", "wm", "size"], serial)
    m = re.search(r"(\d+)x(\d+)", result.stdout.decode(errors="replace"))
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1080, 2400


# ---------------------------------------------------------------------------
# App launch
# ---------------------------------------------------------------------------


def launch_scrcpy(serial: str | None = None) -> subprocess.Popen[bytes]:
    """Start scrcpy in the background for visual feedback during capture."""
    cmd = ["scrcpy", "--window-title", "strivee-mirror", "--stay-awake"]
    if serial:
        cmd += ["-s", serial]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    return proc


def find_strivee_package(serial: str | None = None) -> str:
    """Return the Strivee app package name from the connected device.

    Raises RuntimeError if no Strivee package is found.
    """
    result = _adb(["shell", "pm", "list", "packages"], serial)
    for line in result.stdout.decode(errors="replace").splitlines():
        pkg = line.removeprefix("package:").strip()
        if "strivee" in pkg.lower():
            return pkg
    raise RuntimeError(
        "Strivee not found on device. "
        "Make sure it is installed and the device is connected (adb devices)."
    )


def launch_strivee(serial: str | None = None) -> None:
    """Launch the Strivee app via Android monkey intent."""
    package = find_strivee_package(serial)
    logger.info("Launching %s", package)
    _adb(
        ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
        serial,
    )
    time.sleep(4)


# ---------------------------------------------------------------------------
# Screenshot & navigation
# ---------------------------------------------------------------------------


def take_screenshot(serial: str | None = None) -> Image.Image:
    """Capture the current device screen and return it as an RGB PIL image.

    Raises RuntimeError if ADB returns no data (device disconnected / debugging
    disabled).
    """
    result = _adb(["exec-out", "screencap", "-p"], serial, timeout=10)
    if not result.stdout:
        raise RuntimeError(
            "ADB screenshot returned no data. Is the device connected and USB debugging enabled?"
        )
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def swipe_up(
    serial: str | None = None,
    distance_fraction: float | None = None,
    duration_ms: int = 350,
) -> None:
    """Swipe upward (scroll content down) by a fraction of the screen height."""
    frac = distance_fraction if distance_fraction is not None else config.SCROLL_DISTANCE
    w, h = _device_size(serial)
    cx = w // 2
    y_start = int(h * 0.75)
    y_end = int(h * 0.75 - h * frac)
    _adb(
        ["shell", "input", "swipe", str(cx), str(y_start), str(cx), str(y_end), str(duration_ms)],
        serial,
    )
    time.sleep(0.8)


def swipe_down(
    serial: str | None = None,
    distance_fraction: float | None = None,
    duration_ms: int = 350,
) -> None:
    """Swipe downward (scroll content up) by a fraction of the screen height."""
    frac = distance_fraction if distance_fraction is not None else config.SCROLL_DISTANCE
    w, h = _device_size(serial)
    cx = w // 2
    y_start = int(h * 0.25)
    y_end = int(h * 0.25 + h * frac)
    _adb(
        ["shell", "input", "swipe", str(cx), str(y_start), str(cx), str(y_end), str(duration_ms)],
        serial,
    )
    time.sleep(0.8)


def _change_fraction(a: Image.Image, b: Image.Image) -> float:
    """Fraction of pixels that changed between two screenshots.

    The top 80 px (status bar) are excluded from comparison to avoid false
    positives from the clock ticking.
    """

    def crop(img: Image.Image) -> Image.Image:
        return img.crop((0, 80, img.width, img.height))

    diff = ImageChops.difference(crop(a), crop(b))
    bbox = diff.getbbox()
    if bbox is None:
        return 0.0
    changed = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
    total = a.width * (a.height - 80)
    return changed / total


def _screens_same(a: Image.Image, b: Image.Image) -> bool:
    """Return True when two screenshots are visually identical (< 1% pixel change)."""
    return _change_fraction(a, b) < 0.01


def navigate_to_week(target_week: date, serial: str | None = None) -> None:
    """Swipe the Strivee day-tab strip to reach target_week.

    Assumes Strivee is already open on the current week.
    Finger right → previous week (past).
    Finger left  → next week (future).
    The swipe y-coordinate is placed just inside the bottom of the
    CAPTURE_CROP_TOP area, where the day tabs live.
    """
    today = date.today()
    current_week = today - timedelta(days=today.weekday())
    weeks_diff = (target_week - current_week).days // 7

    if weeks_diff == 0:
        return

    w, h = _device_size(serial)
    cx = w // 2
    # Day tabs sit at the bottom of the header crop zone
    y = config.CAPTURE_CROP_TOP - 50 if config.CAPTURE_CROP_TOP > 0 else int(h * 0.22)
    swipe_distance = w // 3

    direction = "forward" if weeks_diff > 0 else "backward"
    logger.info("Navigating %d week(s) %s to reach %s", abs(weeks_diff), direction, target_week)

    for _ in range(abs(weeks_diff)):
        if weeks_diff < 0:
            # Finger moves right → reveals previous week
            x1, x2 = cx - swipe_distance, cx + swipe_distance
        else:
            # Finger moves left → reveals next week
            x1, x2 = cx + swipe_distance, cx - swipe_distance
        _adb(["shell", "input", "swipe", str(x1), str(y), str(x2), str(y), "300"], serial)
        time.sleep(1.0)


def scroll_to_top(serial: str | None = None, max_swipes: int = 12) -> None:
    """Swipe down repeatedly until the screen stops changing, indicating the top."""
    prev = take_screenshot(serial)
    for _ in range(max_swipes):
        swipe_down(serial)
        curr = take_screenshot(serial)
        if _screens_same(prev, curr):
            break
        prev = curr


def _ui_dump(serial: str | None = None) -> str:
    """Dump the current UI hierarchy XML from the device."""
    _adb(["shell", "uiautomator", "dump", "/sdcard/uidump.xml"], serial)
    result = _adb(["shell", "cat", "/sdcard/uidump.xml"], serial)
    return result.stdout.decode(errors="replace")


def _texts_from_dump(xml_str: str) -> list[str]:
    """Extract non-empty text values from a UI automator XML dump in tree order."""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return []
    return [node.get("text", "").strip() for node in root.iter() if node.get("text", "").strip()]


def _find_element_center(xml_str: str, text: str) -> tuple[int, int] | None:
    """Return the screen centre of the first UI element whose label starts with *text*."""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    for node in root.iter():
        label = node.get("text", "") or node.get("content-desc", "")
        if label.lower().startswith(text.lower()):
            bounds = node.get("bounds", "")
            coords = re.findall(r"\d+", bounds)
            if len(coords) == 4:
                left, top, right, bottom = map(int, coords)
                return (left + right) // 2, (top + bottom) // 2
    return None


def _tap(x: int, y: int, serial: str | None = None) -> None:
    """Tap a point on the device screen and wait for the UI to settle."""
    _adb(["shell", "input", "tap", str(x), str(y)], serial)
    time.sleep(1.2)


def navigate_to_day(day_short: str, serial: str | None = None) -> bool:
    """Tap the day tab in Strivee's agenda view.

    Primary: geometric tap — the week header spans the full screen width with
    7 equally-spaced day columns; Mon is index 0, Sun is index 6.
    Requires CAPTURE_CROP_TOP to be configured (it sets the Y position just
    inside the header). Falls back to UI element detection when not configured.
    """
    try:
        day_index = _WEEKDAYS_EN.index(day_short)
    except ValueError:
        logger.warning("Unknown day '%s'", day_short)
        return False

    w, h = _device_size(serial)

    if config.CAPTURE_CROP_TOP > 0:
        x = int((day_index + 0.5) * w / 7)
        y = config.CAPTURE_CROP_TOP - 50
        logger.debug("Tapping %s at (%d, %d) via geometry", day_short, x, y)
        _tap(x, y, serial)
        return True

    # Fallback: UI element detection (used when CAPTURE_CROP_TOP is not set)
    xml = _ui_dump(serial)
    center = _find_element_center(xml, day_short)
    if center is None:
        fr = _FR_MAP.get(day_short)
        if fr:
            center = _find_element_center(xml, fr)
    if center is None:
        logger.warning("Day tab '%s' not found in UI", day_short)
        return False
    _tap(center[0], center[1], serial)
    return True


# ---------------------------------------------------------------------------
# Multi-scroll text capture
# ---------------------------------------------------------------------------


def capture_day_as_text(
    day_short: str,
    serial: str | None = None,
    max_scrolls: int = 10,
) -> str:
    """Extract all visible programming text for a day via Android accessibility tree.

    Navigates to the day tab, scrolls from top to bottom, and collects unique
    text elements from the UI dump at each position. Returns deduplicated lines
    in appearance order — no screenshots, no stitching, no overlap possible.
    """
    navigate_to_day(day_short, serial)
    time.sleep(0.5)
    scroll_to_top(serial)

    _, h_device = _device_size(serial)
    content_px = h_device - config.CAPTURE_CROP_TOP - config.CAPTURE_CROP_BOTTOM
    scroll_fraction = content_px / h_device

    seen: set[str] = set()
    lines: list[str] = []

    for _ in range(max_scrolls + 1):
        for text in _texts_from_dump(_ui_dump(serial)):
            if text not in seen:
                seen.add(text)
                lines.append(text)
        prev = take_screenshot(serial)
        swipe_up(serial, distance_fraction=scroll_fraction, duration_ms=800)
        curr = take_screenshot(serial)
        if _screens_same(prev, curr):
            break

    logger.debug("%s: UI dump collected %d text elements", day_short, len(lines))
    return "\n".join(lines)
