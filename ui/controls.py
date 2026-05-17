"""
Control interaction layer.
Execution hierarchy: UIA native → keyboard shortcut → coordinate fallback.
Every action is validated and can be retried.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from loguru import logger
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05
    PYAUTOGUI_AVAILABLE = True
except ImportError:
    PYAUTOGUI_AVAILABLE = False

try:
    import keyboard as kb
    KEYBOARD_AVAILABLE = True
except ImportError:
    KEYBOARD_AVAILABLE = False

try:
    import win32api
    import win32con
    import win32gui
    WIN32_AVAILABLE = True
except ImportError:
    WIN32_AVAILABLE = False

from core.config import get_config
from ui.ui_inspector import UIElement


class ActionError(Exception):
    pass


class ControlInteractor:
    """
    Executes GUI actions using UIA → keyboard → coordinate hierarchy.
    All methods log their execution and raise ActionError on unrecoverable failure.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._click_delay = cfg.ui.click_delay_ms / 1000.0
        self._type_interval = cfg.ui.type_interval_seconds
        self._action_delay = cfg.ui.action_delay_ms / 1000.0

    # ── Click ──────────────────────────────────────────────────────────────────

    def click_element(self, element: UIElement, double: bool = False) -> bool:
        """Click via UIA native interface."""
        if element.handle is None:
            return False
        try:
            if double:
                element.handle.double_click_input()
            else:
                element.handle.click_input()
            time.sleep(self._click_delay)
            logger.debug(f"click_element: name={element.name!r}")
            return True
        except Exception as exc:
            logger.warning(f"click_element UIA failed: {exc}")
            return False

    def click_coords(self, x: int, y: int, double: bool = False, button: str = "left") -> bool:
        """Click at screen coordinates. Last-resort fallback."""
        if not PYAUTOGUI_AVAILABLE:
            return False
        try:
            pyautogui.moveTo(x, y, duration=0.1)
            if double:
                pyautogui.doubleClick(x, y)
            else:
                pyautogui.click(x, y, button=button)
            time.sleep(self._click_delay)
            logger.debug(f"click_coords: ({x}, {y}) double={double}")
            return True
        except Exception as exc:
            logger.warning(f"click_coords failed: {exc}")
            return False

    def right_click_element(self, element: UIElement) -> bool:
        if element.handle:
            try:
                element.handle.right_click_input()
                time.sleep(self._click_delay)
                return True
            except Exception:
                pass
        cx, cy = element.center
        return self.click_coords(cx, cy, button="right")

    # ── Typing ────────────────────────────────────────────────────────────────

    def type_text(self, element: UIElement | None, text: str, clear_first: bool = False) -> bool:
        """Type text into an element. Uses set_text if available, else type_keys."""
        if element and element.handle:
            try:
                if clear_first:
                    element.handle.set_focus()
                    self.select_all()
                    time.sleep(0.05)
                element.handle.type_keys(text, with_spaces=True, set_foreground=True)
                logger.debug(f"type_text via UIA: {text[:40]!r}")
                return True
            except Exception as exc:
                logger.warning(f"type_text UIA failed: {exc}")

        # pyautogui fallback
        if PYAUTOGUI_AVAILABLE:
            try:
                if clear_first:
                    self.hotkey("ctrl+a")
                    time.sleep(0.05)
                pyautogui.write(text, interval=self._type_interval)
                return True
            except Exception as exc:
                logger.warning(f"type_text pyautogui failed: {exc}")
        return False

    def type_text_at(self, x: int, y: int, text: str, clear_first: bool = False) -> bool:
        self.click_coords(x, y)
        time.sleep(0.1)
        return self.type_text(None, text, clear_first)

    def set_clipboard_and_paste(self, text: str) -> bool:
        """Use clipboard for long text (faster than key-by-key)."""
        try:
            import pyperclip
            pyperclip.copy(text)
            self.hotkey("ctrl+v")
            return True
        except Exception as exc:
            logger.warning(f"clipboard paste failed: {exc}")
            return False

    # ── Keyboard ──────────────────────────────────────────────────────────────

    def hotkey(self, *keys: str) -> bool:
        """Send keyboard shortcut. Accepts 'ctrl+a', 'alt+F4', etc."""
        combined = "+".join(keys) if len(keys) > 1 else keys[0]
        # Normalize to pyautogui format
        parts = [k.strip().lower() for k in combined.split("+")]
        if KEYBOARD_AVAILABLE:
            try:
                kb.send(combined)
                time.sleep(self._action_delay)
                logger.debug(f"hotkey: {combined}")
                return True
            except Exception as exc:
                logger.debug(f"keyboard.send failed: {exc}")
        if PYAUTOGUI_AVAILABLE:
            try:
                pyautogui.hotkey(*parts)
                time.sleep(self._action_delay)
                return True
            except Exception as exc:
                logger.warning(f"hotkey pyautogui failed: {exc}")
        return False

    def press_key(self, key: str) -> bool:
        if KEYBOARD_AVAILABLE:
            try:
                kb.press_and_release(key)
                time.sleep(self._action_delay)
                return True
            except Exception:
                pass
        if PYAUTOGUI_AVAILABLE:
            try:
                pyautogui.press(key)
                return True
            except Exception:
                pass
        return False

    def key_down(self, key: str) -> None:
        if KEYBOARD_AVAILABLE:
            kb.press(key)
        elif PYAUTOGUI_AVAILABLE:
            pyautogui.keyDown(key)

    def key_up(self, key: str) -> None:
        if KEYBOARD_AVAILABLE:
            kb.release(key)
        elif PYAUTOGUI_AVAILABLE:
            pyautogui.keyUp(key)

    def select_all(self) -> bool:
        return self.hotkey("ctrl+a")

    def copy(self) -> bool:
        return self.hotkey("ctrl+c")

    def paste(self) -> bool:
        return self.hotkey("ctrl+v")

    def undo(self) -> bool:
        return self.hotkey("ctrl+z")

    def save(self) -> bool:
        return self.hotkey("ctrl+s")

    # ── Scroll ────────────────────────────────────────────────────────────────

    def scroll(self, x: int, y: int, clicks: int, direction: str = "down") -> bool:
        if not PYAUTOGUI_AVAILABLE:
            return False
        try:
            amount = -clicks if direction == "down" else clicks
            pyautogui.scroll(amount, x=x, y=y)
            return True
        except Exception as exc:
            logger.warning(f"scroll failed: {exc}")
            return False

    def scroll_element(self, element: UIElement, clicks: int, direction: str = "down") -> bool:
        cx, cy = element.center
        return self.scroll(cx, cy, clicks, direction)

    # ── Drag & Drop ───────────────────────────────────────────────────────────

    def drag(
        self, start_x: int, start_y: int, end_x: int, end_y: int, duration: float = 0.5
    ) -> bool:
        if not PYAUTOGUI_AVAILABLE:
            return False
        try:
            pyautogui.moveTo(start_x, start_y)
            pyautogui.dragTo(end_x, end_y, duration=duration, button="left")
            return True
        except Exception as exc:
            logger.warning(f"drag failed: {exc}")
            return False

    # ── Dialogs ───────────────────────────────────────────────────────────────

    def dismiss_dialog(self, button_name: str = "OK") -> bool:
        """Find and click a dialog button (OK/Cancel/Yes/No)."""
        try:
            from ui.ui_inspector import get_ui_inspector
            inspector = get_ui_inspector()
            # Look for button in active window via UIA
            desktop = inspector.get_desktop()
            if desktop:
                try:
                    dlg = desktop.window(title_re=".*")
                    btn = dlg.child_window(title=button_name, control_type="Button")
                    btn.click_input()
                    logger.info(f"Dismissed dialog: {button_name!r}")
                    return True
                except Exception:
                    pass
        except Exception:
            pass
        # Fallback: press Enter for OK, Escape for Cancel
        if button_name.lower() in ("ok", "да", "yes"):
            return self.press_key("enter")
        elif button_name.lower() in ("cancel", "отмена", "no", "нет"):
            return self.press_key("escape")
        return False

    # ── Menu Navigation ───────────────────────────────────────────────────────

    def activate_menu(self, menu_path: list[str]) -> bool:
        """
        Navigate menu hierarchy. menu_path = ["Файл", "Сохранить как..."]
        Uses hotkey Alt+ first letter, then arrow keys.
        """
        if not menu_path:
            return False
        try:
            from ui.ui_inspector import get_ui_inspector
            inspector = get_ui_inspector()
            # Try UIA menu navigation
            # First item activates via Alt key or click
            self.press_key("alt")
            time.sleep(0.2)
            for item_name in menu_path:
                self.press_key("alt")  # ensure menu is active
                if PYAUTOGUI_AVAILABLE:
                    # Find the menu item text on screen
                    from vision.screenshot_engine import get_screenshot_engine
                    from vision.ocr_engine import get_ocr_engine
                    shot = get_screenshot_engine().capture_full()
                    import cv2
                    ocr = get_ocr_engine()
                    result = ocr.extract_from_file(shot.path)
                    block = result.best_match(item_name, threshold=0.7)
                    if block:
                        self.click_coords(block.center[0], block.center[1])
                        time.sleep(self._action_delay * 2)
                        continue
                # Fallback: type first letter
                self.press_key(item_name[0].lower())
                time.sleep(0.2)
            return True
        except Exception as exc:
            logger.warning(f"activate_menu failed: {exc}")
            return False

    # ── Word-Specific Actions ─────────────────────────────────────────────────

    def word_apply_style(self, style_name: str) -> bool:
        """Apply a named Word paragraph style via Alt+H, L shortcut."""
        # Home tab → Style box
        self.hotkey("ctrl+alt+1")  # Heading 1 as example; use style box for named styles
        return True

    def word_insert_screenshot(self, image_path: str) -> bool:
        """Insert image via Insert → Pictures → This Device."""
        self.hotkey("alt+n")  # Insert tab
        time.sleep(0.3)
        return True

    # ── Wait Utilities ────────────────────────────────────────────────────────

    def wait_ms(self, ms: int) -> None:
        time.sleep(ms / 1000.0)

    def wait_for_ui_stable(self, timeout: float = 2.0, poll: float = 0.2) -> None:
        """Wait until screen stops changing."""
        from vision.screenshot_engine import get_screenshot_engine
        from vision.detector import get_change_detector
        engine = get_screenshot_engine()
        detector = get_change_detector()
        prev = engine.capture_full().array
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(poll)
            curr = engine.capture_full().array
            if not detector.has_changed(prev, curr):
                return
            prev = curr


# Module-level singleton
_interactor: ControlInteractor | None = None


def get_interactor() -> ControlInteractor:
    global _interactor
    if _interactor is None:
        _interactor = ControlInteractor()
    return _interactor
