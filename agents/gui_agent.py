"""
GUI Execution Agent.
Executes atomic GUI actions using the three-tier fallback hierarchy:
  1. Native UI Automation (pywinauto UIA)
  2. Keyboard shortcuts
  3. Coordinate-based fallback (OCR-located + pyautogui)

Every action is logged, validated, and retried on transient failure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from loguru import logger
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from core.config import get_config
from ui.controls import ControlInteractor, ActionError, get_interactor
from ui.ui_inspector import UIElement, get_ui_inspector
from ui.window_manager import get_window_manager
from agents.vision_agent import VisionAgent, get_vision_agent


class ActionStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


@dataclass
class ActionResult:
    status: ActionStatus
    action: str
    target: str
    duration_ms: float = 0.0
    retry_count: int = 0
    error: str = ""
    screenshot_path: str = ""

    @property
    def ok(self) -> bool:
        return self.status == ActionStatus.SUCCESS


class TransientError(Exception):
    """Retryable failure (element not found, timing issue)."""


class FatalError(Exception):
    """Non-retryable failure (wrong application, critical state error)."""


class GUIAgent:
    """
    Executes GUI actions against Word and Excel.
    Implements full Word and Excel operation library.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._cfg = cfg
        self._ctrl = get_interactor()
        self._inspector = get_ui_inspector()
        self._wm = get_window_manager()
        self._vision = get_vision_agent()
        self._max_retries = cfg.recovery.max_action_retries

    # ─────────────────────────────────────────────────────────────────────────
    # Core Execution Framework
    # ─────────────────────────────────────────────────────────────────────────

    def execute(self, action: str, **kwargs) -> ActionResult:
        """
        Dispatch and execute a named action.
        Maps action strings to implementation methods.
        """
        t0 = time.time()
        handler = self._dispatch(action)
        if handler is None:
            return ActionResult(
                status=ActionStatus.FAILED,
                action=action,
                target=kwargs.get("target", ""),
                error=f"Unknown action: {action}",
            )
        retry_count = 0
        last_error = ""
        while retry_count <= self._max_retries:
            try:
                handler(**kwargs)
                duration = (time.time() - t0) * 1000
                logger.info(
                    f"Action OK | {action} | target={kwargs.get('target', '')!r} "
                    f"| {duration:.0f}ms | retries={retry_count}"
                )
                return ActionResult(
                    status=ActionStatus.SUCCESS,
                    action=action,
                    target=kwargs.get("target", ""),
                    duration_ms=duration,
                    retry_count=retry_count,
                )
            except TransientError as exc:
                last_error = str(exc)
                retry_count += 1
                wait = min(0.5 * (2 ** retry_count), 8.0)
                logger.warning(
                    f"Action retry {retry_count}/{self._max_retries} | {action}: {exc}"
                )
                time.sleep(wait)
            except FatalError as exc:
                last_error = str(exc)
                logger.error(f"Fatal action error | {action}: {exc}")
                break
            except Exception as exc:
                last_error = str(exc)
                retry_count += 1
                logger.warning(f"Unexpected error in {action}: {exc}")
                time.sleep(0.5)

        # Capture failure state
        fail_shot = self._vision.capture_failure_state(action)
        return ActionResult(
            status=ActionStatus.FAILED,
            action=action,
            target=kwargs.get("target", ""),
            duration_ms=(time.time() - t0) * 1000,
            retry_count=retry_count,
            error=last_error,
            screenshot_path=str(fail_shot.path),
        )

    def _dispatch(self, action: str):
        handlers = {
            # Application control
            "open_word": self.open_word,
            "open_excel": self.open_excel,
            "open_file": self.open_file,
            "close_file": self.close_file,
            "save_file": self.save_file,
            "save_as": self.save_as,
            # Navigation
            "click": self.click,
            "focus_window": self.focus_app_window,
            "scroll": self.scroll,
            # Text
            "type": self.type_text,
            "select_text": self.select_text,
            "select_all": self.select_all,
            "delete_text": self.delete_text,
            # Formatting
            "format_text": self.format_text,
            "apply_style": self.apply_style,
            "set_font": self.set_font,
            "set_font_size": self.set_font_size,
            "bold": self.bold,
            "italic": self.italic,
            "underline": self.underline,
            "align": self.align_text,
            "set_line_spacing": self.set_line_spacing,
            # Insertion
            "insert_table": self.insert_table,
            "insert_image": self.insert_image,
            "insert_page_break": self.insert_page_break,
            "insert_toc": self.insert_toc,
            # Menu navigation
            "menu_navigate": self.menu_navigate,
            "hotkey": self.hotkey,
            # Excel specific
            "click_cell": self.excel_click_cell,
            "enter_formula": self.excel_enter_formula,
            "create_chart": self.excel_create_chart,
            "auto_sum": self.excel_auto_sum,
            "format_cells": self.excel_format_cells,
            # Verification trigger
            "wait_ms": self.wait_ms,
        }
        return handlers.get(action)

    # ─────────────────────────────────────────────────────────────────────────
    # Application Control
    # ─────────────────────────────────────────────────────────────────────────

    def open_word(self, file_path: str = "", **_) -> None:
        proc = self._wm.launch_word(file_path or None)
        if proc is None:
            raise FatalError("Cannot launch Word — executable not found")
        win = self._wm.wait_for_window("Word", timeout=20.0)
        if not win:
            raise TransientError("Word window did not appear")
        self._wm.maximize_window(win.hwnd)
        time.sleep(1.0)  # Let Word finish loading

    def open_excel(self, file_path: str = "", **_) -> None:
        proc = self._wm.launch_excel(file_path or None)
        if proc is None:
            raise FatalError("Cannot launch Excel — executable not found")
        win = self._wm.wait_for_window("Excel", timeout=20.0)
        if not win:
            raise TransientError("Excel window did not appear")
        self._wm.maximize_window(win.hwnd)
        time.sleep(1.0)

    def open_file(self, target: str = "", application: str = "word", **_) -> None:
        """Open a file by path in the target application."""
        if application == "word":
            self.open_word(file_path=target)
        elif application == "excel":
            self.open_excel(file_path=target)
        else:
            raise FatalError(f"Unknown application: {application}")

    def close_file(self, save: bool = True, **_) -> None:
        if save:
            self._ctrl.hotkey("ctrl+s")
            time.sleep(0.5)
        self._ctrl.hotkey("ctrl+w")
        time.sleep(0.3)

    def save_file(self, **_) -> None:
        self._ctrl.hotkey("ctrl+s")
        time.sleep(0.8)
        # Dismiss any save dialogs
        self._dismiss_save_dialogs()

    def save_as(self, target: str = "", **_) -> None:
        self._ctrl.hotkey("ctrl+shift+s")
        time.sleep(0.5)
        if target:
            self._type_in_dialog("Имя файла", target)
            self._ctrl.press_key("enter")
            time.sleep(1.0)

    def focus_app_window(self, target: str = "word", **_) -> None:
        if target == "word":
            win = self._wm.find_word_window()
        elif target == "excel":
            win = self._wm.find_excel_window()
        else:
            win = self._wm.find_by_title(target)
        if win:
            self._wm.focus_window(win.hwnd)
        else:
            raise TransientError(f"Window not found: {target}")

    # ─────────────────────────────────────────────────────────────────────────
    # Navigation & Clicks
    # ─────────────────────────────────────────────────────────────────────────

    def click(self, target: str = "", double: bool = False, **_) -> None:
        """Click a UI element by name. Tries UIA first, then OCR-located coords."""
        # UIA attempt
        app = self._get_active_app()
        if app:
            elem = self._find_element_smart(app, target)
            if elem:
                if self._ctrl.click_element(elem, double=double):
                    return
        # OCR fallback
        det = self._vision.find_button_on_screen(target)
        if det.found:
            cx, cy = det.center
            self._ctrl.click_coords(cx, cy, double=double)
            return
        raise TransientError(f"Element not found: {target!r}")

    def scroll(self, target: str = "down", clicks: int = 3, **_) -> None:
        state = self._vision.capture_state(include_ui_tree=False, include_ocr=False)
        shot = state.screenshot
        if shot:
            cx, cy = shot.width // 2, shot.height // 2
        else:
            cx, cy = 960, 540
        self._ctrl.scroll(cx, cy, clicks, direction=target)

    # ─────────────────────────────────────────────────────────────────────────
    # Text Operations
    # ─────────────────────────────────────────────────────────────────────────

    def type_text(self, value: str = "", target: str = "", clear_first: bool = False, **_) -> None:
        """Type text at current cursor position or into named element."""
        if target:
            self.click(target)
            time.sleep(0.1)
        elem = None
        if target:
            app = self._get_active_app()
            if app:
                elem = self._find_element_smart(app, target)

        # Use clipboard for long text
        if len(value) > 200:
            if not self._ctrl.set_clipboard_and_paste(value):
                self._ctrl.type_text(elem, value[:200], clear_first=clear_first)
        else:
            if not self._ctrl.type_text(elem, value, clear_first=clear_first):
                raise TransientError(f"Typing failed for: {value[:40]!r}")

    def select_text(self, target: str = "", method: str = "all", **_) -> None:
        if method == "all":
            self.select_all()
        elif method == "word":
            self._ctrl.hotkey("ctrl+shift+right")
        elif method == "line":
            self._ctrl.hotkey("shift+end")
        elif method == "paragraph":
            self._ctrl.hotkey("ctrl+shift+down")

    def select_all(self, **_) -> None:
        self._ctrl.select_all()

    def delete_text(self, **_) -> None:
        self._ctrl.press_key("delete")

    # ─────────────────────────────────────────────────────────────────────────
    # Text Formatting
    # ─────────────────────────────────────────────────────────────────────────

    def format_text(self, target: str = "", value: str = "", **_) -> None:
        """Apply format to selected text. value = 'bold', 'italic', etc."""
        fmt_map = {
            "bold": "ctrl+b",
            "italic": "ctrl+i",
            "underline": "ctrl+u",
            "bold+italic": "ctrl+b+i",
        }
        hotkey = fmt_map.get(value.lower(), "")
        if hotkey:
            self._ctrl.hotkey(hotkey)
        else:
            logger.warning(f"Unknown format: {value}")

    def bold(self, **_) -> None:
        self._ctrl.hotkey("ctrl+b")

    def italic(self, **_) -> None:
        self._ctrl.hotkey("ctrl+i")

    def underline(self, **_) -> None:
        self._ctrl.hotkey("ctrl+u")

    def apply_style(self, value: str = "", target: str = "", **_) -> None:
        """Apply a named paragraph style via Word style box."""
        # Word shortcut: Ctrl+Shift+S opens style dialog; or use the ribbon
        self._ctrl.hotkey("ctrl+shift+s")
        time.sleep(0.4)
        # Type style name in the Apply Styles dialog
        app = self._get_active_app()
        if app:
            try:
                style_field = app.window(title="Применить стили").Edit
                style_field.set_text(value)
                self._ctrl.press_key("enter")
                return
            except Exception:
                pass
        # Fallback: type style name and press Enter in style box
        self._ctrl.type_text(None, value, clear_first=True)
        self._ctrl.press_key("enter")

    def set_font(self, value: str = "", **_) -> None:
        """Set font family for selected text."""
        # Ctrl+Shift+F opens font dialog in Word
        self._ctrl.hotkey("ctrl+shift+f")
        time.sleep(0.3)
        self._ctrl.select_all()
        self._ctrl.type_text(None, value)
        self._ctrl.press_key("enter")

    def set_font_size(self, value: str = "", **_) -> None:
        """Set font size for selected text."""
        self._ctrl.hotkey("ctrl+shift+p")
        time.sleep(0.3)
        self._ctrl.select_all()
        self._ctrl.type_text(None, str(value))
        self._ctrl.press_key("enter")

    def align_text(self, value: str = "left", **_) -> None:
        align_map = {
            "left": "ctrl+l",
            "center": "ctrl+e",
            "right": "ctrl+r",
            "justify": "ctrl+j",
            "по левому краю": "ctrl+l",
            "по центру": "ctrl+e",
            "по правому краю": "ctrl+r",
            "по ширине": "ctrl+j",
        }
        hotkey = align_map.get(value.lower(), "ctrl+l")
        self._ctrl.hotkey(hotkey)

    def set_line_spacing(self, value: str = "1.5", **_) -> None:
        spacing_map = {
            "1": "ctrl+1",
            "1.5": "ctrl+5",
            "2": "ctrl+2",
            "single": "ctrl+1",
            "double": "ctrl+2",
        }
        hotkey = spacing_map.get(str(value), "ctrl+5")
        self._ctrl.hotkey(hotkey)

    # ─────────────────────────────────────────────────────────────────────────
    # Insertion
    # ─────────────────────────────────────────────────────────────────────────

    def insert_table(self, rows: int = 3, cols: int = 3, **_) -> None:
        """Insert table via Insert > Table menu."""
        self._ctrl.hotkey("alt+n")       # Insert tab
        time.sleep(0.2)
        self._ctrl.hotkey("alt+n+t")     # Table button
        time.sleep(0.3)
        # Use OCR to find table grid or dialog
        state = self._vision.capture_state(include_ui_tree=False)
        if state.has_text("Вставить таблицу") or state.has_text("Insert Table"):
            # Table dialog: fill rows and columns
            self._type_in_dialog("Число столбцов", str(cols))
            self._type_in_dialog("Число строк", str(rows))
            self._ctrl.dismiss_dialog("OK")
        else:
            # Grid selector: move mouse to select NxM grid
            # Navigate using arrow keys: cols right, rows down, then Enter
            for _ in range(cols - 1):
                self._ctrl.press_key("right")
            for _ in range(rows - 1):
                self._ctrl.press_key("down")
            self._ctrl.press_key("enter")

    def insert_image(self, target: str = "", **_) -> None:
        """Insert image from file path."""
        # Alt+N, P = Insert > Picture > This Device
        self._ctrl.hotkey("alt+n")
        time.sleep(0.2)
        # Try to click "Рисунки" / "Pictures" on Insert tab
        det = self._vision.find_button_on_screen("Рисунки")
        if not det.found:
            det = self._vision.find_button_on_screen("Изображения")
        if det.found:
            self._ctrl.click_coords(*det.center)
            time.sleep(0.3)
        # Dialog "Вставка рисунка" / "Insert Picture"
        det2 = self._vision.find_button_on_screen("Это устройство")
        if not det2.found:
            det2 = self._vision.find_button_on_screen("This Device")
        if det2.found:
            self._ctrl.click_coords(*det2.center)
            time.sleep(0.3)
        # Type file path in file dialog
        if target:
            self._ctrl.type_text(None, target)
            self._ctrl.press_key("enter")
            time.sleep(1.0)

    def insert_page_break(self, **_) -> None:
        self._ctrl.hotkey("ctrl+enter")

    def insert_toc(self, **_) -> None:
        """Insert Table of Contents via References tab."""
        self._ctrl.hotkey("alt+s")   # References tab (in Russian: Ссылки = Alt+S)
        time.sleep(0.2)
        # Click "Оглавление" / "Table of Contents"
        det = self._vision.find_button_on_screen("Оглавление")
        if not det.found:
            det = self._vision.find_button_on_screen("Table of Contents")
        if det.found:
            self._ctrl.click_coords(*det.center)
            time.sleep(0.3)
            # Select automatic TOC
            det2 = self._vision.find_button_on_screen("Автособираемое оглавление")
            if not det2.found:
                det2 = self._vision.find_button_on_screen("Automatic Table")
            if det2.found:
                self._ctrl.click_coords(*det2.center)

    # ─────────────────────────────────────────────────────────────────────────
    # Menu Navigation
    # ─────────────────────────────────────────────────────────────────────────

    def menu_navigate(self, target: str = "", value: str = "", **_) -> None:
        """Navigate menu by path string. target = 'Файл/Сохранить как'."""
        parts = [p.strip() for p in target.replace("→", "/").split("/")]
        self._ctrl.activate_menu(parts)

    def hotkey(self, value: str = "", target: str = "", **_) -> None:
        """Send keyboard shortcut. value = 'ctrl+s', etc."""
        key_str = value or target
        if key_str:
            self._ctrl.hotkey(key_str)

    def wait_ms(self, value: int = 500, **_) -> None:
        time.sleep(value / 1000.0)

    # ─────────────────────────────────────────────────────────────────────────
    # Excel-Specific Actions
    # ─────────────────────────────────────────────────────────────────────────

    def excel_click_cell(self, target: str = "", **_) -> None:
        """Click an Excel cell by address (e.g., 'A1', 'B3')."""
        # Use Name Box: Ctrl+G or type address in name box
        self._ctrl.hotkey("ctrl+g")  # Go To dialog
        time.sleep(0.2)
        self._ctrl.type_text(None, target, clear_first=True)
        self._ctrl.press_key("enter")

    def excel_enter_formula(self, target: str = "", value: str = "", **_) -> None:
        """Enter a formula in a cell. target = cell address, value = formula."""
        if target:
            self.excel_click_cell(target)
        if value:
            self._ctrl.type_text(None, value)
            self._ctrl.press_key("enter")

    def excel_create_chart(
        self,
        chart_type: str = "column",
        data_range: str = "",
        **_,
    ) -> None:
        """Create chart from selected data."""
        if data_range:
            self.excel_click_cell(data_range.split(":")[0])
            # Select range
            self._ctrl.hotkey("ctrl+shift+end")
        # Insert > Chart
        self._ctrl.hotkey("alt+n")     # Insert tab
        time.sleep(0.2)
        det = self._vision.find_button_on_screen("Диаграмма")
        if not det.found:
            det = self._vision.find_button_on_screen("Chart")
        if det.found:
            self._ctrl.click_coords(*det.center)
            time.sleep(0.5)
            self._ctrl.press_key("enter")   # Accept default chart type

    def excel_auto_sum(self, target: str = "", **_) -> None:
        """Apply AutoSum to current cell/range."""
        if target:
            self.excel_click_cell(target)
        self._ctrl.hotkey("alt+=")
        self._ctrl.press_key("enter")

    def excel_format_cells(self, value: str = "", **_) -> None:
        """Open Format Cells dialog."""
        self._ctrl.hotkey("ctrl+1")
        time.sleep(0.3)
        if value:
            # Navigate to the appropriate tab
            det = self._vision.find_button_on_screen(value)
            if det.found:
                self._ctrl.click_coords(*det.center)
            self._ctrl.dismiss_dialog("OK")

    # ─────────────────────────────────────────────────────────────────────────
    # Private Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _get_active_app(self):
        """Return pywinauto app handle for the currently active Office app."""
        state = self._vision.capture_state(include_ui_tree=False, include_ocr=False)
        if state.detected_application == "word":
            return self._inspector.find_word(timeout=3.0)
        elif state.detected_application == "excel":
            return self._inspector.find_excel(timeout=3.0)
        return None

    def _find_element_smart(self, app, name: str) -> UIElement | None:
        """Try multiple strategies to find a UI element."""
        if not app:
            return None
        try:
            # 1: exact name
            elem = self._inspector.find_element(app, title=name, timeout=3.0)
            if elem:
                return elem
            # 2: partial name match
            elem = self._inspector.find_element_by_name_in_tree(app, name, partial=True)
            if elem:
                return elem
        except Exception as exc:
            logger.debug(f"_find_element_smart failed: {exc}")
        return None

    def _type_in_dialog(self, field_label: str, value: str) -> bool:
        """Find a labeled input field in a dialog and type into it."""
        app = self._get_active_app()
        if app:
            elem = self._find_element_smart(app, field_label)
            if elem:
                self._ctrl.click_element(elem)
                self._ctrl.select_all()
                self._ctrl.type_text(elem, value)
                return True
        # OCR fallback
        det = self._vision.find_button_on_screen(field_label)
        if det.found:
            # Click just to the right of the label (where input is)
            self._ctrl.click_coords(det.x + det.width + 50, det.center[1])
            self._ctrl.select_all()
            self._ctrl.type_text(None, value)
            return True
        return False

    def _dismiss_save_dialogs(self) -> None:
        """Handle common save confirmation dialogs."""
        state = self._vision.capture_state(include_ui_tree=False)
        for dialog in state.detected_dialogs:
            d_lower = dialog.lower()
            if any(k in d_lower for k in ["сохранить", "save", "заменить"]):
                self._ctrl.dismiss_dialog("Да")
                time.sleep(0.3)
            elif "format" in d_lower or "формат" in d_lower:
                self._ctrl.dismiss_dialog("Да")
                time.sleep(0.3)


# Module-level singleton
_agent: GUIAgent | None = None


def get_gui_agent() -> GUIAgent:
    global _agent
    if _agent is None:
        _agent = GUIAgent()
    return _agent
