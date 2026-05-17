"""
Popup and dialog handler.
Runs as background poll or on-demand scan.
Handles Office autosave dialogs, update dialogs, error boxes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from loguru import logger

from core.config import get_config


@dataclass
class PopupInfo:
    title: str
    content: str
    action_taken: str
    timestamp: float


# Known dialog patterns and auto-responses
DIALOG_RULES: list[dict] = [
    # Word/Excel autosave/recovery dialogs
    {
        "title_patterns": ["восстановление документа", "document recovery"],
        "content_patterns": [],
        "action": "close",   # just close recovery panel
        "button": "Закрыть",
    },
    # Save format warning (keep DOCX format)
    {
        "title_patterns": ["microsoft word", "microsoft excel"],
        "content_patterns": ["формат", "format", "сохранить как", "keep"],
        "action": "confirm",
        "button": "Да",
    },
    # Unsaved changes on exit
    {
        "title_patterns": ["microsoft word", "microsoft excel"],
        "content_patterns": ["сохранить", "save changes", "хотите сохранить"],
        "action": "save",
        "button": "Сохранить",
    },
    # Update/activation dialogs — dismiss
    {
        "title_patterns": ["обновление", "update", "активация", "activation", "лицензия"],
        "content_patterns": [],
        "action": "dismiss",
        "button": "Закрыть",
    },
    # Generic error boxes
    {
        "title_patterns": ["ошибка", "error", "предупреждение", "warning"],
        "content_patterns": [],
        "action": "ok",
        "button": "OK",
    },
    # Compatibility mode warning
    {
        "title_patterns": ["режим совместимости", "compatibility mode"],
        "content_patterns": [],
        "action": "ok",
        "button": "OK",
    },
    # Clipboard warning
    {
        "title_patterns": ["буфер обмена", "clipboard"],
        "content_patterns": [],
        "action": "ok",
        "button": "Нет",
    },
]


class PopupHandler:
    """
    Monitors for and dismisses known popup dialogs.
    Can run continuously in background or be called on-demand.
    """

    def __init__(
        self,
        interval: float | None = None,
        on_popup: Callable[[PopupInfo], None] | None = None,
    ) -> None:
        cfg = get_config()
        self._interval = interval or cfg.recovery.popup_check_interval_seconds
        self._on_popup = on_popup
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        self._dismissed: list[PopupInfo] = []

    def start(self) -> None:
        if self._running:
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="popup_handler", daemon=True
        )
        self._thread.start()
        logger.info("PopupHandler started")

    def stop(self) -> None:
        self._stop_event.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("PopupHandler stopped")

    def check_once(self) -> list[PopupInfo]:
        """Synchronously check for and dismiss popups. Returns list of dismissed."""
        dismissed = []
        try:
            windows = self._get_all_windows()
            for hwnd, title in windows:
                info = self._evaluate_window(hwnd, title)
                if info:
                    dismissed.append(info)
                    self._dismissed.append(info)
                    if self._on_popup:
                        self._on_popup(info)
        except Exception as exc:
            logger.debug(f"Popup check failed: {exc}")
        return dismissed

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                dismissed = self.check_once()
                if dismissed:
                    logger.info(f"PopupHandler dismissed {len(dismissed)} dialogs")
            except Exception as exc:
                logger.error(f"PopupHandler loop error: {exc}")
            self._stop_event.wait(self._interval)

    def _get_all_windows(self) -> list[tuple[int, str]]:
        windows = []
        try:
            import win32gui
            def callback(hwnd, _):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title:
                        windows.append((hwnd, title))
                return True
            win32gui.EnumWindows(callback, None)
        except Exception:
            pass
        return windows

    def _evaluate_window(self, hwnd: int, title: str) -> PopupInfo | None:
        """Check if a window matches any popup rule and act."""
        title_lower = title.lower()

        # Skip main application windows
        skip_patterns = [
            "microsoft word", "microsoft excel",
            "visual studio", "chrome", "firefox", "explorer",
        ]
        # Only skip if title looks like a full app window (long title)
        if len(title) > 60:
            return None

        for rule in DIALOG_RULES:
            # Check title matches
            title_match = not rule["title_patterns"] or any(
                p in title_lower for p in rule["title_patterns"]
            )
            if not title_match:
                continue

            # Get window content
            content = self._get_window_text(hwnd)
            content_lower = content.lower()

            # Check content matches
            content_match = not rule["content_patterns"] or any(
                p in content_lower for p in rule["content_patterns"]
            )
            if not content_match:
                continue

            # Match found — act on it
            action = rule["action"]
            button = rule["button"]
            logger.info(f"PopupHandler: handling dialog '{title}' action={action}")
            self._act(hwnd, action, button)
            return PopupInfo(
                title=title,
                content=content[:200],
                action_taken=f"{action}:{button}",
                timestamp=time.time(),
            )
        return None

    def _get_window_text(self, hwnd: int) -> str:
        """Get all text from child controls of a window."""
        texts = []
        try:
            import win32gui
            def callback(child_hwnd, _):
                text = win32gui.GetWindowText(child_hwnd)
                if text:
                    texts.append(text)
                return True
            win32gui.EnumChildWindows(hwnd, callback, None)
        except Exception:
            pass
        return " ".join(texts)

    def _act(self, hwnd: int, action: str, button: str) -> None:
        """Perform the action on the dialog."""
        try:
            import win32gui
            import win32con

            # Focus the window
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.1)

            if action in ("ok", "confirm", "save", "dismiss"):
                self._click_button_in_window(hwnd, button)
            elif action == "close":
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            else:
                self._click_button_in_window(hwnd, button)
        except Exception as exc:
            # Fallback: press Enter
            logger.debug(f"_act failed ({exc}), trying keyboard Enter")
            try:
                import win32api
                win32api.keybd_event(0x0D, 0, 0, 0)  # Enter down
                win32api.keybd_event(0x0D, 0, 0x0002, 0)  # Enter up
            except Exception:
                pass

    def _click_button_in_window(self, hwnd: int, button_text: str) -> bool:
        """Find and click a button in a window by its text."""
        try:
            import win32gui
            import win32con

            result = {"found": False}

            def callback(child_hwnd, _):
                text = win32gui.GetWindowText(child_hwnd)
                cls = win32gui.GetClassName(child_hwnd)
                if (
                    text.lower() == button_text.lower()
                    or text.startswith(button_text[:4])
                ) and "Button" in cls:
                    win32gui.SendMessage(child_hwnd, win32con.BM_CLICK, 0, 0)
                    result["found"] = True
                return True

            win32gui.EnumChildWindows(hwnd, callback, None)
            return result["found"]
        except Exception as exc:
            logger.debug(f"_click_button_in_window failed: {exc}")
            return False

    @property
    def dismissed_count(self) -> int:
        return len(self._dismissed)


# Module-level singleton
_handler: PopupHandler | None = None


def get_popup_handler() -> PopupHandler:
    global _handler
    if _handler is None:
        _handler = PopupHandler()
    return _handler
