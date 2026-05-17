"""
Window lifecycle management: find, focus, resize, maximize, position.
Wraps Win32 API and pywinauto for reliable cross-DPI window control.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

try:
    import ctypes
    import ctypes.wintypes as wt
    import win32con
    import win32gui
    import win32process
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

from core.config import get_config


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    rect_left: int = 0
    rect_top: int = 0
    rect_right: int = 0
    rect_bottom: int = 0
    is_visible: bool = True
    is_minimized: bool = False

    @property
    def width(self) -> int:
        return self.rect_right - self.rect_left

    @property
    def height(self) -> int:
        return self.rect_bottom - self.rect_top

    @property
    def center(self) -> tuple[int, int]:
        return (
            self.rect_left + self.width // 2,
            self.rect_top + self.height // 2,
        )


class WindowManager:
    """
    Manages application windows: launch, focus, resize, enumerate.
    Uses Win32 API for lowest-level reliability.
    """

    WORD_EXE = r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE"
    EXCEL_EXE = r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"

    def __init__(self) -> None:
        cfg = get_config()
        self._focus_wait_ms = cfg.ui.focus_wait_ms

    # ── Discovery ─────────────────────────────────────────────────────────────

    def enumerate_windows(self) -> list[WindowInfo]:
        """Return all visible top-level windows."""
        if not WIN32_AVAILABLE:
            return []
        windows: list[WindowInfo] = []

        def callback(hwnd: int, _: Any) -> bool:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            cls = win32gui.GetClassName(hwnd)
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            rect = win32gui.GetWindowRect(hwnd)
            windows.append(WindowInfo(
                hwnd=hwnd,
                title=title,
                class_name=cls,
                pid=pid,
                rect_left=rect[0],
                rect_top=rect[1],
                rect_right=rect[2],
                rect_bottom=rect[3],
                is_visible=True,
                is_minimized=win32gui.IsIconic(hwnd) != 0,
            ))
            return True

        win32gui.EnumWindows(callback, None)
        return windows

    def find_by_title(self, partial_title: str) -> WindowInfo | None:
        """Find first window whose title contains partial_title."""
        for win in self.enumerate_windows():
            if partial_title.lower() in win.title.lower():
                return win
        return None

    def find_word_window(self) -> WindowInfo | None:
        patterns = ["word", ".doc", "документ"]
        for pat in patterns:
            win = self.find_by_title(pat)
            if win:
                return win
        return None

    def find_excel_window(self) -> WindowInfo | None:
        patterns = ["excel", ".xls", "книга", "лист"]
        for pat in patterns:
            win = self.find_by_title(pat)
            if win:
                return win
        return None

    # ── Focus & Activation ───────────────────────────────────────────────────

    def focus_window(self, hwnd: int) -> bool:
        if not WIN32_AVAILABLE:
            return False
        try:
            # Restore if minimized
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.2)
            # Bring to foreground
            win32gui.SetForegroundWindow(hwnd)
            # Additional: use AllowSetForegroundWindow for stubborn cases
            ctypes.windll.user32.AllowSetForegroundWindow(ctypes.wintypes.DWORD(-1))
            win32gui.BringWindowToTop(hwnd)
            time.sleep(self._focus_wait_ms / 1000.0)
            actual = win32gui.GetForegroundWindow()
            if actual == hwnd:
                return True
            # Fallback: Alt key trick to unlock foreground
            import win32api
            win32api.keybd_event(0x12, 0, 0, 0)       # Alt down
            win32api.keybd_event(0x12, 0, 0x0002, 0)  # Alt up
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.1)
            return win32gui.GetForegroundWindow() == hwnd
        except Exception as exc:
            logger.warning(f"focus_window({hwnd}) failed: {exc}")
            return False

    def focus_by_title(self, partial_title: str) -> bool:
        win = self.find_by_title(partial_title)
        if win:
            return self.focus_window(win.hwnd)
        return False

    # ── Resize & Position ─────────────────────────────────────────────────────

    def maximize_window(self, hwnd: int) -> None:
        if WIN32_AVAILABLE:
            win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)

    def restore_window(self, hwnd: int) -> None:
        if WIN32_AVAILABLE:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    def move_window(self, hwnd: int, x: int, y: int, w: int, h: int) -> None:
        if WIN32_AVAILABLE:
            win32gui.MoveWindow(hwnd, x, y, w, h, True)

    def center_window(self, hwnd: int, screen_w: int = 1920, screen_h: int = 1080) -> None:
        """Center a window on primary monitor."""
        if WIN32_AVAILABLE:
            rect = win32gui.GetWindowRect(hwnd)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            x = (screen_w - w) // 2
            y = (screen_h - h) // 2
            win32gui.MoveWindow(hwnd, x, y, w, h, True)

    # ── Application Launch ────────────────────────────────────────────────────

    def launch_word(self, file_path: str | None = None) -> subprocess.Popen | None:
        return self._launch_office(self.WORD_EXE, file_path, "Word")

    def launch_excel(self, file_path: str | None = None) -> subprocess.Popen | None:
        return self._launch_office(self.EXCEL_EXE, file_path, "Excel")

    def _launch_office(
        self, exe: str, file_path: str | None, app_name: str
    ) -> subprocess.Popen | None:
        if not Path(exe).exists():
            # Try alternate paths
            for alt in [
                r"C:\Program Files (x86)\Microsoft Office\root\Office16",
                r"C:\Program Files\Microsoft Office\Office16",
            ]:
                candidate = Path(alt) / Path(exe).name
                if candidate.exists():
                    exe = str(candidate)
                    break
            else:
                logger.error(f"{app_name} executable not found: {exe}")
                return None
        cmd = [exe]
        if file_path:
            cmd.append(file_path)
        try:
            proc = subprocess.Popen(cmd)
            logger.info(f"Launched {app_name}: PID={proc.pid}")
            return proc
        except Exception as exc:
            logger.error(f"Failed to launch {app_name}: {exc}")
            return None

    def wait_for_window(
        self, title_fragment: str, timeout: float = 20.0, poll: float = 1.0
    ) -> WindowInfo | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            win = self.find_by_title(title_fragment)
            if win and not win.is_minimized:
                logger.info(f"Window appeared: {win.title!r}")
                return win
            time.sleep(poll)
        logger.warning(f"Timed out waiting for window: {title_fragment!r}")
        return None

    # ── Process Management ────────────────────────────────────────────────────

    def kill_process(self, pid: int) -> bool:
        if not PSUTIL_AVAILABLE:
            return False
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            proc.wait(timeout=5)
            return True
        except Exception as exc:
            logger.warning(f"kill_process({pid}) failed: {exc}")
            try:
                psutil.Process(pid).kill()
                return True
            except Exception:
                return False

    def is_process_alive(self, pid: int) -> bool:
        if not PSUTIL_AVAILABLE:
            return False
        try:
            return psutil.Process(pid).is_running()
        except psutil.NoSuchProcess:
            return False

    def find_process_by_name(self, name: str) -> list[int]:
        if not PSUTIL_AVAILABLE:
            return []
        result = []
        for proc in psutil.process_iter(["pid", "name"]):
            if name.lower() in (proc.info.get("name") or "").lower():
                result.append(proc.info["pid"])
        return result

    def get_memory_mb(self, pid: int) -> float:
        if not PSUTIL_AVAILABLE:
            return 0.0
        try:
            return psutil.Process(pid).memory_info().rss / 1024 / 1024
        except Exception:
            return 0.0


# Module-level singleton
_wm: WindowManager | None = None


def get_window_manager() -> WindowManager:
    global _wm
    if _wm is None:
        _wm = WindowManager()
    return _wm
