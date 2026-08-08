"""Windows 11 DWM acrylic backdrop for the floating overlay windows.

Best-effort only. DWMWA_SYSTEMBACKDROP_TYPE needs Windows 11 (reliably from
build 22621+), and its interaction with a colour-keyed, always-on-top,
click-through Toplevel (see bar_overlay.py's _set_clickthrough) is genuinely
untested territory -- every call here is wrapped so a failure (older
Windows, the API missing, anything) just leaves the existing flat panel
rather than crashing the overlay.
"""

import ctypes
import os
from ctypes import wintypes

_DWMWA_SYSTEMBACKDROP_TYPE = 38
# DWM_SYSTEMBACKDROP_TYPE enum: 0 Auto, 1 None, 2 Mica (DWMSBT_MAINWINDOW),
# 3 Acrylic (DWMSBT_TRANSIENTWINDOW), 4 Mica Alt (DWMSBT_TABBEDWINDOW).
# Acrylic is the one that actually reads as translucent/frosted; Mica is
# opaque and tints toward the desktop wallpaper instead.
_DWMSBT_TRANSIENTWINDOW = 3


class _Margins(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int),
    ]


def try_enable_acrylic(hwnd: int) -> bool:
    """Extends the DWM frame across the whole client area and requests an
    Acrylic system backdrop for `hwnd`. Returns whether both calls reported
    success -- callers should treat a False return as "stays a flat panel",
    never as an error to surface."""
    if os.name != "nt" or not hwnd:
        return False
    try:
        dwmapi = ctypes.windll.dwmapi
        margins = _Margins(-1, -1, -1, -1)
        ok_margins = dwmapi.DwmExtendFrameIntoClientArea(
            wintypes.HWND(hwnd), ctypes.byref(margins)
        ) == 0
        backdrop = ctypes.c_int(_DWMSBT_TRANSIENTWINDOW)
        ok_backdrop = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), _DWMWA_SYSTEMBACKDROP_TYPE,
            ctypes.byref(backdrop), ctypes.sizeof(backdrop),
        ) == 0
        return ok_margins and ok_backdrop
    except Exception:
        return False
