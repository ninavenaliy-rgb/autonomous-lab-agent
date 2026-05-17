"""
Screenshot capture engine.
Primary: mss (fast, direct DX/GDI capture).
Provides ROI detection, auto-crop, and image optimization.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import cv2
import numpy as np
from loguru import logger
from PIL import Image

try:
    import mss
    import mss.tools
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False
    logger.warning("mss not available; falling back to pyautogui")

try:
    import pyautogui
    PYAUTOGUI_AVAILABLE = True
except ImportError:
    PYAUTOGUI_AVAILABLE = False

from core.config import get_config


@dataclass
class Region:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def as_mss_monitor(self) -> dict:
        return {"left": self.x, "top": self.y, "width": self.width, "height": self.height}

    def as_bbox(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.right, self.bottom)

    def with_padding(self, px: int) -> "Region":
        return Region(
            x=max(0, self.x - px),
            y=max(0, self.y - px),
            width=self.width + 2 * px,
            height=self.height + 2 * px,
        )


@dataclass
class Screenshot:
    path: Path
    width: int
    height: int
    region: Region | None
    taken_at: float
    figure_number: int | None = None
    caption: str = ""

    @property
    def array(self) -> np.ndarray:
        return cv2.imread(str(self.path))

    @property
    def pil(self) -> Image.Image:
        return Image.open(self.path)


class ScreenshotEngine:
    """
    Captures screenshots using mss (primary) or pyautogui (fallback).
    Handles DPI scaling, ROI cropping, and quality optimization.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._output_dir = cfg.storage.screenshot_dir
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._format = cfg.vision.screenshot_format
        self._quality = cfg.vision.screenshot_quality
        self._dpi_scale = cfg.vision.dpi_scale_factor
        self._roi_padding = cfg.vision.roi_padding

        if MSS_AVAILABLE:
            self._sct = mss.mss()
            # Auto-detect DPI scale from primary monitor
            self._detect_dpi_scale()
        else:
            self._sct = None

    def _detect_dpi_scale(self) -> None:
        """Detect Windows DPI scaling factor."""
        try:
            import ctypes
            awareness = ctypes.c_int()
            ctypes.windll.shcore.GetProcessDpiAwareness(0, ctypes.byref(awareness))
            # Set per-monitor DPI awareness
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            cfg = get_config()
            if cfg.vision.dpi_scale_factor == 1.0:
                hdc = ctypes.windll.user32.GetDC(0)
                dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
                ctypes.windll.user32.ReleaseDC(0, hdc)
                self._dpi_scale = dpi / 96.0
                logger.debug(f"DPI scale detected: {self._dpi_scale:.2f}")
        except Exception:
            self._dpi_scale = 1.0

    def _filename(self, prefix: str = "shot") -> Path:
        ts = int(time.time() * 1000)
        uid = uuid.uuid4().hex[:6]
        return self._output_dir / f"{prefix}_{ts}_{uid}.{self._format}"

    def capture_full(self, prefix: str = "full") -> Screenshot:
        """Capture the entire primary monitor."""
        path = self._filename(prefix)
        t0 = time.time()

        if MSS_AVAILABLE and self._sct:
            monitor = self._sct.monitors[1]  # [0] = all-monitors composite
            img = np.array(self._sct.grab(monitor))
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            region = Region(
                x=monitor["left"],
                y=monitor["top"],
                width=monitor["width"],
                height=monitor["height"],
            )
        elif PYAUTOGUI_AVAILABLE:
            pil_img = pyautogui.screenshot()
            img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
            h, w = img.shape[:2]
            region = Region(0, 0, w, h)
        else:
            raise RuntimeError("No screenshot backend available")

        self._save(img, path)
        h, w = img.shape[:2]
        shot = Screenshot(path=path, width=w, height=h, region=region, taken_at=t0)
        logger.debug(f"Full screenshot: {path.name} ({w}x{h})")
        return shot

    def capture_region(self, region: Region, prefix: str = "region") -> Screenshot:
        """Capture a specific screen region."""
        path = self._filename(prefix)
        t0 = time.time()

        # Scale region by DPI factor to get real pixel coords
        scaled = Region(
            x=int(region.x * self._dpi_scale),
            y=int(region.y * self._dpi_scale),
            width=int(region.width * self._dpi_scale),
            height=int(region.height * self._dpi_scale),
        )

        if MSS_AVAILABLE and self._sct:
            img = np.array(self._sct.grab(scaled.as_mss_monitor()))
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        elif PYAUTOGUI_AVAILABLE:
            pil_img = pyautogui.screenshot(region=scaled.as_bbox())
            img = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
        else:
            raise RuntimeError("No screenshot backend available")

        self._save(img, path)
        h, w = img.shape[:2]
        return Screenshot(path=path, width=w, height=h, region=region, taken_at=t0)

    def capture_window(
        self, hwnd: int | None = None, window_title: str | None = None, prefix: str = "window"
    ) -> Screenshot:
        """Capture a specific window by HWND or title."""
        region = self._get_window_region(hwnd, window_title)
        if region:
            padded = region.with_padding(self._roi_padding)
            return self.capture_region(padded, prefix)
        return self.capture_full(prefix)

    def _get_window_region(
        self, hwnd: int | None, title: str | None
    ) -> Region | None:
        try:
            import ctypes
            import ctypes.wintypes as wt

            if hwnd is None and title:
                hwnd = ctypes.windll.user32.FindWindowW(None, title)
            if not hwnd:
                return None

            rect = wt.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
            return Region(
                x=rect.left,
                y=rect.top,
                width=rect.right - rect.left,
                height=rect.bottom - rect.top,
            )
        except Exception as exc:
            logger.debug(f"Window region detection failed: {exc}")
            return None

    def crop_to_content(self, shot: Screenshot, threshold: int = 240) -> Screenshot:
        """Auto-crop whitespace borders from a screenshot."""
        img = shot.array
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        mask = gray < threshold
        rows = np.any(mask, axis=1)
        cols = np.any(mask, axis=0)
        if not rows.any() or not cols.any():
            return shot
        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]
        pad = self._roi_padding
        rmin = max(0, rmin - pad)
        rmax = min(img.shape[0], rmax + pad)
        cmin = max(0, cmin - pad)
        cmax = min(img.shape[1], cmax + pad)
        cropped = img[rmin:rmax, cmin:cmax]
        path = self._filename("cropped")
        self._save(cropped, path)
        h, w = cropped.shape[:2]
        return Screenshot(
            path=path,
            width=w,
            height=h,
            region=shot.region,
            taken_at=shot.taken_at,
            figure_number=shot.figure_number,
            caption=shot.caption,
        )

    def optimize_for_report(self, shot: Screenshot, max_width: int = 1200) -> Screenshot:
        """Resize and optimize image for embedding in a DOCX report."""
        img = shot.array
        h, w = img.shape[:2]
        if w > max_width:
            scale = max_width / w
            new_w = max_width
            new_h = int(h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
        path = self._filename("report")
        self._save(img, path)
        h2, w2 = img.shape[:2]
        return Screenshot(
            path=path,
            width=w2,
            height=h2,
            region=shot.region,
            taken_at=shot.taken_at,
            figure_number=shot.figure_number,
            caption=shot.caption,
        )

    def capture_failure(self, context: str = "") -> Screenshot:
        """Capture a diagnostic screenshot on failure."""
        prefix = f"FAILURE_{context[:20].replace(' ', '_')}" if context else "FAILURE"
        shot = self.capture_full(prefix)
        logger.warning(f"Failure screenshot saved: {shot.path.name}")
        return shot

    def _save(self, img: np.ndarray, path: Path) -> None:
        if self._format == "png":
            cv2.imwrite(str(path), img, [cv2.IMWRITE_PNG_COMPRESSION, 3])
        else:
            cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, self._quality])

    def close(self) -> None:
        if self._sct:
            self._sct.close()


# Module-level singleton
_engine: ScreenshotEngine | None = None


def get_screenshot_engine() -> ScreenshotEngine:
    global _engine
    if _engine is None:
        _engine = ScreenshotEngine()
    return _engine
