"""
Windows UI Automation API inspector.
Primary layer for all GUI interaction — avoids coordinate brittle-ness.
Uses pywinauto UIA backend with graceful fallback detection.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from loguru import logger

try:
    import pywinauto
    from pywinauto import Application, Desktop
    from pywinauto.controls.uiawrapper import UIAWrapper
    from pywinauto.findwindows import ElementNotFoundError, find_elements
    PYWINAUTO_AVAILABLE = True
except ImportError:
    PYWINAUTO_AVAILABLE = False
    logger.warning("pywinauto not available — UIA inspection disabled")

from core.config import get_config


@dataclass
class UIElement:
    """Normalized UI element representation."""
    handle: Any = None          # pywinauto wrapper
    name: str = ""
    control_type: str = ""
    automation_id: str = ""
    class_name: str = ""
    is_enabled: bool = True
    is_visible: bool = True
    rect_left: int = 0
    rect_top: int = 0
    rect_right: int = 0
    rect_bottom: int = 0
    children_count: int = 0
    value: str = ""
    help_text: str = ""

    @property
    def center(self) -> tuple[int, int]:
        return (
            (self.rect_left + self.rect_right) // 2,
            (self.rect_top + self.rect_bottom) // 2,
        )

    @property
    def width(self) -> int:
        return self.rect_right - self.rect_left

    @property
    def height(self) -> int:
        return self.rect_bottom - self.rect_top

    def click(self) -> None:
        if self.handle and PYWINAUTO_AVAILABLE:
            self.handle.click_input()

    def type_keys(self, text: str) -> None:
        if self.handle and PYWINAUTO_AVAILABLE:
            self.handle.type_keys(text, with_spaces=True)

    def set_text(self, text: str) -> None:
        if self.handle and PYWINAUTO_AVAILABLE:
            self.handle.set_text(text)

    def get_text(self) -> str:
        if self.handle and PYWINAUTO_AVAILABLE:
            try:
                return self.handle.window_text() or ""
            except Exception:
                return ""
        return self.value


@dataclass
class UITree:
    """Snapshot of UI state for a window or desktop."""
    root_element: UIElement | None = None
    elements: list[UIElement] = field(default_factory=list)
    captured_at: float = field(default_factory=time.time)

    def find_by_name(self, name: str, partial: bool = False) -> list[UIElement]:
        if partial:
            return [e for e in self.elements if name.lower() in e.name.lower()]
        return [e for e in self.elements if e.name == name]

    def find_by_type(self, control_type: str) -> list[UIElement]:
        ct = control_type.lower()
        return [e for e in self.elements if e.control_type.lower() == ct]

    def find_by_automation_id(self, aid: str) -> UIElement | None:
        for e in self.elements:
            if e.automation_id == aid:
                return e
        return None

    def find_by_class(self, class_name: str) -> list[UIElement]:
        return [e for e in self.elements if class_name in e.class_name]

    def buttons(self) -> list[UIElement]:
        return self.find_by_type("Button")

    def text_inputs(self) -> list[UIElement]:
        return self.find_by_type("Edit")

    def menu_items(self) -> list[UIElement]:
        return self.find_by_type("MenuItem")


class UIInspector:
    """
    Inspects Windows desktop using UI Automation.
    Enumerates windows, elements, and properties.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._timeout = cfg.ui.find_element_timeout_seconds
        self._backend = "uia"

    def get_desktop(self) -> Desktop | None:
        if not PYWINAUTO_AVAILABLE:
            return None
        try:
            return Desktop(backend=self._backend)
        except Exception as exc:
            logger.error(f"Desktop init failed: {exc}")
            return None

    def list_windows(self) -> list[str]:
        """Return titles of all visible top-level windows."""
        if not PYWINAUTO_AVAILABLE:
            return []
        try:
            desktop = self.get_desktop()
            if desktop is None:
                return []
            return [w.window_text() for w in desktop.windows() if w.is_visible()]
        except Exception as exc:
            logger.error(f"list_windows failed: {exc}")
            return []

    def find_window(self, title: str | None = None, class_name: str | None = None,
                    partial_title: bool = True, timeout: float | None = None) -> Application | None:
        if not PYWINAUTO_AVAILABLE:
            return None
        t = timeout or self._timeout
        deadline = time.time() + t
        while time.time() < deadline:
            try:
                kwargs: dict[str, Any] = {"backend": self._backend}
                if title and partial_title:
                    kwargs["title_re"] = f".*{title}.*"
                elif title:
                    kwargs["title"] = title
                if class_name:
                    kwargs["class_name"] = class_name
                app = Application(backend=self._backend).connect(**kwargs)
                return app
            except Exception:
                time.sleep(0.5)
        logger.warning(f"Window not found in {t}s: title={title!r}")
        return None

    def find_word(self, timeout: float | None = None) -> Application | None:
        """Find Microsoft Word window."""
        patterns = ["Microsoft Word", "Word", ".docx", "Документ"]
        for pat in patterns:
            app = self.find_window(title=pat, timeout=2.0)
            if app:
                logger.debug(f"Word found via pattern: {pat!r}")
                return app
        return None

    def find_excel(self, timeout: float | None = None) -> Application | None:
        """Find Microsoft Excel window."""
        patterns = ["Microsoft Excel", "Excel", ".xlsx", "Книга"]
        for pat in patterns:
            app = self.find_window(title=pat, timeout=2.0)
            if app:
                logger.debug(f"Excel found via pattern: {pat!r}")
                return app
        return None

    def get_active_window(self) -> UIElement | None:
        """Return the currently focused window."""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if hwnd:
                return self._hwnd_to_element(hwnd)
        except Exception as exc:
            logger.debug(f"get_active_window failed: {exc}")
        return None

    def _hwnd_to_element(self, hwnd: int) -> UIElement | None:
        try:
            import ctypes
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(length)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length)
            name = buf.value
            return UIElement(handle=None, name=name)
        except Exception:
            return None

    def find_element(
        self,
        app: "Application",
        control_type: str | None = None,
        title: str | None = None,
        automation_id: str | None = None,
        class_name: str | None = None,
        timeout: float | None = None,
    ) -> UIElement | None:
        if not PYWINAUTO_AVAILABLE or app is None:
            return None
        t = timeout or self._timeout
        deadline = time.time() + t
        while time.time() < deadline:
            try:
                kwargs: dict[str, Any] = {}
                if control_type:
                    kwargs["control_type"] = control_type
                if title:
                    kwargs["title"] = title
                if automation_id:
                    kwargs["auto_id"] = automation_id
                if class_name:
                    kwargs["class_name"] = class_name
                wrapper = app.window(**kwargs).wrapper_object()
                return self._wrap(wrapper)
            except ElementNotFoundError:
                time.sleep(0.3)
            except Exception as exc:
                logger.debug(f"find_element error: {exc}")
                time.sleep(0.3)
        return None

    def find_element_by_name_in_tree(
        self, app: "Application", name: str, partial: bool = True
    ) -> UIElement | None:
        """DFS search for element by name anywhere in the UI tree."""
        if not PYWINAUTO_AVAILABLE or app is None:
            return None
        try:
            dlg = app.top_window()
            if partial:
                element = dlg.child_window(title_re=f".*{name}.*")
            else:
                element = dlg.child_window(title=name)
            wrapper = element.wrapper_object()
            return self._wrap(wrapper)
        except Exception as exc:
            logger.debug(f"find_element_by_name_in_tree: {exc}")
            return None

    def dump_tree(self, app: "Application", depth: int = 5) -> UITree:
        """Build a UITree snapshot for the top window."""
        if not PYWINAUTO_AVAILABLE or app is None:
            return UITree()
        elements = []
        try:
            dlg = app.top_window()
            self._collect_elements(dlg.wrapper_object(), elements, depth, 0)
        except Exception as exc:
            logger.debug(f"dump_tree failed: {exc}")
        return UITree(elements=elements)

    def _collect_elements(
        self, wrapper: "UIAWrapper", out: list[UIElement], max_depth: int, current_depth: int
    ) -> None:
        if current_depth > max_depth:
            return
        try:
            elem = self._wrap(wrapper)
            if elem:
                out.append(elem)
            for child in wrapper.children():
                self._collect_elements(child, out, max_depth, current_depth + 1)
        except Exception:
            pass

    def _wrap(self, wrapper: "UIAWrapper") -> UIElement | None:
        try:
            rect = wrapper.rectangle()
            return UIElement(
                handle=wrapper,
                name=wrapper.window_text() or "",
                control_type=wrapper.friendly_class_name() or "",
                automation_id=getattr(wrapper, "automation_id", lambda: "")() if callable(getattr(wrapper, "automation_id", None)) else "",
                class_name=wrapper.class_name() or "",
                is_enabled=wrapper.is_enabled(),
                is_visible=wrapper.is_visible(),
                rect_left=rect.left,
                rect_top=rect.top,
                rect_right=rect.right,
                rect_bottom=rect.bottom,
                children_count=len(wrapper.children()),
            )
        except Exception:
            return None

    def wait_for_element(
        self,
        condition: Callable[[], UIElement | None],
        timeout: float = 15.0,
        poll_interval: float = 0.5,
        description: str = "element",
    ) -> UIElement | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = condition()
            if result:
                return result
            time.sleep(poll_interval)
        logger.warning(f"Timed out waiting for: {description}")
        return None

    def is_dialog_open(self, app: "Application", title: str) -> bool:
        if not PYWINAUTO_AVAILABLE or app is None:
            return False
        try:
            dlg = app.window(title_re=f".*{title}.*")
            return dlg.exists(timeout=1.0)
        except Exception:
            return False


# Module-level singleton
_inspector: UIInspector | None = None


def get_ui_inspector() -> UIInspector:
    global _inspector
    if _inspector is None:
        _inspector = UIInspector()
    return _inspector
