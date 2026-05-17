"""
Desktop Vision Agent.
Perceives current desktop state: active windows, visible controls, text, dialogs.
Combines UIA inspection + OCR + visual change detection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger

try:
    import numpy as np
except ImportError:
    np = None  # type: ignore

from core.config import get_config
from ui.ui_inspector import UIElement, UITree, get_ui_inspector
from ui.window_manager import WindowInfo, get_window_manager
from vision.screenshot_engine import Screenshot, get_screenshot_engine
from vision.ocr_engine import OCRResult, get_ocr_engine
from vision.detector import DetectionResult, get_template_detector, get_change_detector


@dataclass
class DesktopState:
    """Complete perception of the current desktop state."""
    timestamp: float = field(default_factory=time.time)
    screenshot: Screenshot | None = None
    active_window_title: str = ""
    active_window_hwnd: int = 0
    visible_windows: list[WindowInfo] = field(default_factory=list)
    ui_tree: UITree | None = None
    ocr_result: OCRResult | None = None
    detected_application: str = "none"  # word / excel / none
    detected_dialogs: list[str] = field(default_factory=list)
    is_ready: bool = True

    @property
    def screen_text(self) -> str:
        if self.ocr_result:
            return self.ocr_result.full_text
        return ""

    def has_text(self, pattern: str) -> bool:
        if self.ocr_result:
            return bool(self.ocr_result.find_text(pattern))
        return False

    def find_ui_element(self, name: str) -> UIElement | None:
        if self.ui_tree:
            matches = self.ui_tree.find_by_name(name, partial=True)
            return matches[0] if matches else None
        return None


@dataclass
class VerificationResult:
    """Result of a state verification check."""
    passed: bool
    description: str
    evidence: str = ""
    screenshot: Screenshot | None = None


class VisionAgent:
    """
    Observes and understands the desktop.
    Combines UI Automation, OCR, and visual analysis.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._inspector = get_ui_inspector()
        self._wm = get_window_manager()
        self._screen = get_screenshot_engine()
        self._ocr = get_ocr_engine()
        self._template = get_template_detector()
        self._change = get_change_detector()
        self._last_state: DesktopState | None = None

    def capture_state(self, include_ocr: bool = True, include_ui_tree: bool = True) -> DesktopState:
        """Full desktop state snapshot."""
        t0 = time.time()
        state = DesktopState()

        # Screenshot
        try:
            state.screenshot = self._screen.capture_full()
        except Exception as exc:
            logger.warning(f"Screenshot failed: {exc}")

        # Active window
        try:
            active = self._inspector.get_active_window()
            if active:
                state.active_window_title = active.name
        except Exception:
            pass

        # Window enumeration
        try:
            state.visible_windows = self._wm.enumerate_windows()
        except Exception:
            pass

        # Detect application
        state.detected_application = self._detect_application(state)

        # UI tree (expensive — only when needed)
        if include_ui_tree:
            try:
                app_handle = None
                if "word" in state.detected_application:
                    app_handle = self._inspector.find_word(timeout=2.0)
                elif "excel" in state.detected_application:
                    app_handle = self._inspector.find_excel(timeout=2.0)
                if app_handle:
                    state.ui_tree = self._inspector.dump_tree(app_handle)
            except Exception as exc:
                logger.debug(f"UI tree capture failed: {exc}")

        # OCR
        if include_ocr and state.screenshot:
            try:
                state.ocr_result = self._ocr.extract_from_file(state.screenshot.path)
            except Exception as exc:
                logger.debug(f"OCR failed: {exc}")

        # Detect dialogs
        state.detected_dialogs = self._detect_dialogs(state)

        duration = (time.time() - t0) * 1000
        logger.debug(
            f"Desktop state captured in {duration:.0f}ms "
            f"| app={state.detected_application} "
            f"| dialogs={state.detected_dialogs}"
        )
        self._last_state = state
        return state

    def _detect_application(self, state: DesktopState) -> str:
        """Determine which application is active."""
        title = state.active_window_title.lower()
        for win in state.visible_windows:
            t = win.title.lower()
            if any(k in t for k in ["microsoft word", "winword", ".doc"]):
                return "word"
            if any(k in t for k in ["microsoft excel", "excel", ".xls"]):
                return "excel"
        if any(k in title for k in ["word", ".doc", "документ"]):
            return "word"
        if any(k in title for k in ["excel", ".xls", "таблиц", "книга"]):
            return "excel"
        return "none"

    def _detect_dialogs(self, state: DesktopState) -> list[str]:
        """Detect open dialog boxes."""
        dialogs = []
        dialog_patterns = [
            "сохранить", "save", "открыть", "open",
            "предупреждение", "warning", "ошибка", "error",
            "подтвердите", "confirm", "microsoft word", "microsoft excel",
            "да", "нет", "отмена",
        ]
        for win in state.visible_windows:
            title_lower = win.title.lower()
            if any(p in title_lower for p in dialog_patterns):
                # Small windows are likely dialogs
                if win.width < 800 and win.height < 600:
                    dialogs.append(win.title)
        return dialogs

    # ── Verification Methods ───────────────────────────────────────────────────

    def verify_application_open(self, app: str) -> VerificationResult:
        """Check that Word or Excel is open and focused."""
        state = self.capture_state(include_ui_tree=False)
        detected = state.detected_application
        if detected == app or app in state.active_window_title.lower():
            return VerificationResult(
                passed=True,
                description=f"{app} is open",
                evidence=state.active_window_title,
            )
        # Check all windows
        for win in state.visible_windows:
            if app.lower() in win.title.lower():
                return VerificationResult(
                    passed=True,
                    description=f"{app} found in window list",
                    evidence=win.title,
                )
        return VerificationResult(
            passed=False,
            description=f"{app} not detected",
            evidence=f"Active: {state.active_window_title}",
        )

    def verify_text_visible(self, text: str, screenshot: Screenshot | None = None) -> VerificationResult:
        """Verify that specific text is visible on screen."""
        if screenshot is None:
            shot = self._screen.capture_full()
        else:
            shot = screenshot
        import cv2
        img = cv2.imread(str(shot.path))
        result = self._ocr.extract_from_array(img)
        match = result.best_match(text, threshold=0.7)
        if match:
            return VerificationResult(
                passed=True,
                description=f"Text '{text}' found on screen",
                evidence=f"OCR: '{match.text}' at ({match.x}, {match.y})",
                screenshot=shot,
            )
        return VerificationResult(
            passed=False,
            description=f"Text '{text}' not found on screen",
            screenshot=shot,
        )

    def verify_element_exists(
        self, element_name: str, app: str = "word"
    ) -> VerificationResult:
        """Check that a UI element with given name exists."""
        state = self.capture_state()
        elem = state.find_ui_element(element_name)
        if elem:
            return VerificationResult(
                passed=True,
                description=f"Element '{element_name}' found",
                evidence=f"type={elem.control_type}",
            )
        # Fallback to OCR
        text_result = self.verify_text_visible(element_name, state.screenshot)
        return text_result

    def verify_dialog_dismissed(self, dialog_title: str) -> VerificationResult:
        """Verify a specific dialog is no longer open."""
        state = self.capture_state(include_ui_tree=False, include_ocr=False)
        still_open = any(
            dialog_title.lower() in d.lower() for d in state.detected_dialogs
        )
        if still_open:
            return VerificationResult(
                passed=False,
                description=f"Dialog '{dialog_title}' still open",
            )
        return VerificationResult(
            passed=True,
            description=f"Dialog '{dialog_title}' dismissed",
        )

    def verify_screen_changed(
        self, before: Screenshot, after: Screenshot | None = None
    ) -> VerificationResult:
        """Verify that the screen changed after an action."""
        if after is None:
            after = self._screen.capture_full()
        import cv2
        b = cv2.imread(str(before.path))
        a = cv2.imread(str(after.path))
        changes = self._change.detect_changes(b, a)
        if changes:
            largest = max(changes, key=lambda c: c.width * c.height)
            return VerificationResult(
                passed=True,
                description=f"Screen changed: {len(changes)} regions",
                evidence=f"Largest change: {largest.width}x{largest.height}",
                screenshot=after,
            )
        similarity = self._change.similarity(b, a)
        return VerificationResult(
            passed=False,
            description=f"Screen unchanged (similarity={similarity:.2f})",
            screenshot=after,
        )

    def wait_for_condition(
        self,
        condition_fn,
        timeout: float = 15.0,
        poll: float = 0.5,
        description: str = "condition",
    ) -> VerificationResult:
        """Poll until condition_fn() returns a passing VerificationResult."""
        deadline = time.time() + timeout
        last_result: VerificationResult | None = None
        while time.time() < deadline:
            try:
                result = condition_fn()
                if result.passed:
                    logger.debug(f"Condition met: {description}")
                    return result
                last_result = result
            except Exception as exc:
                logger.debug(f"Condition check error: {exc}")
            time.sleep(poll)
        if last_result:
            return last_result
        return VerificationResult(
            passed=False,
            description=f"Timeout waiting for: {description}",
        )

    def find_button_on_screen(self, label: str) -> DetectionResult:
        """Find a button by label using OCR."""
        shot = self._screen.capture_full()
        import cv2
        img = cv2.imread(str(shot.path))
        result = self._ocr.extract_from_array(img)
        block = result.best_match(label, threshold=0.65)
        if block:
            return DetectionResult(
                found=True,
                x=block.x, y=block.y,
                width=block.width, height=block.height,
                confidence=block.confidence,
                label=block.text,
            )
        return DetectionResult(found=False)

    def get_word_document_content(self) -> str:
        """Extract visible text from Word document area via OCR."""
        shot = self._screen.capture_full("word_content")
        import cv2
        img = cv2.imread(str(shot.path))
        result = self._ocr.extract_from_array(img)
        return result.full_text

    def capture_failure_state(self, context: str = "") -> Screenshot:
        """Capture diagnostic screenshot for failure analysis."""
        return self._screen.capture_failure(context)


# Module-level singleton
_agent: VisionAgent | None = None


def get_vision_agent() -> "VisionAgent":
    global _agent
    if _agent is None:
        import platform
        if platform.system() != "Windows":
            raise RuntimeError("VisionAgent requires Windows GUI (headless mode: not available)")
        _agent = VisionAgent()
    return _agent
