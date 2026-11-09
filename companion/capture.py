"""Windows 창 캡처. PrintWindow를 쓰고 검게 나오면 화면 영역 복사로 대신한다."""

from __future__ import annotations

import ctypes
import io

import win32con
import win32gui
import win32ui
from PIL import Image

PW_RENDERFULLCONTENT = 0x00000002


def find_window(keyword: str) -> tuple[int | None, str]:
    """제목에 keyword가 들어가는 보이는 최상위 창 가운데 가장 큰 것을 고른다."""
    keyword_l = keyword.lower()
    found: list[tuple[int, int, str]] = []

    def enum(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if title and keyword_l in title.lower():
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            found.append(((right - left) * (bottom - top), hwnd, title))
        return True

    win32gui.EnumWindows(enum, None)
    if not found:
        return None, ""
    found.sort(reverse=True)
    _, hwnd, title = found[0]
    return hwnd, title


def capture_window_png(hwnd: int) -> bytes | None:
    if win32gui.IsIconic(hwnd):
        return None
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0:
        return None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(bitmap)
    try:
        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if not ok:
            save_dc.BitBlt((0, 0), (width, height), mfc_dc, (0, 0), win32con.SRCCOPY)
        info = bitmap.GetInfo()
        data = bitmap.GetBitmapBits(True)
        image = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), data, "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)

    if _is_blank(image):
        return None
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _is_blank(image: Image.Image) -> bool:
    small = image.resize((32, 32))
    pixels = list(small.getdata())
    first = pixels[0]
    return all(p == first for p in pixels)
