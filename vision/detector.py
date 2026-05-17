"""
Visual detection: template matching, contour detection, change detection.
Used as fallback when UIA element lookup fails.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from loguru import logger

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    cv2 = None  # type: ignore
    np = None   # type: ignore

from core.config import get_config


@dataclass
class DetectionResult:
    found: bool
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    confidence: float = 0.0
    label: str = ""

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    @property
    def not_found(self) -> bool:
        return not self.found


class TemplateDetector:
    """Template matching for finding UI elements by appearance."""

    def __init__(self, threshold: float | None = None) -> None:
        cfg = get_config()
        self._threshold = threshold or cfg.vision.template_match_threshold

    def find(
        self,
        haystack,
        template,
        method: int = 5,  # cv2.TM_CCOEFF_NORMED = 5
    ) -> DetectionResult:
        """Find template in haystack image."""
        if haystack is None or template is None:
            return DetectionResult(found=False)
        if template.shape[0] > haystack.shape[0] or template.shape[1] > haystack.shape[1]:
            return DetectionResult(found=False)

        # Convert to grayscale for matching
        h_gray = cv2.cvtColor(haystack, cv2.COLOR_BGR2GRAY) if len(haystack.shape) == 3 else haystack
        t_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY) if len(template.shape) == 3 else template

        result = cv2.matchTemplate(h_gray, t_gray, method)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

        if method in (cv2.TM_SQDIFF, cv2.TM_SQDIFF_NORMED):
            best_val = min_val
            best_loc = min_loc
        else:
            best_val = max_val
            best_loc = max_loc

        th, tw = template.shape[:2]
        if best_val >= self._threshold:
            return DetectionResult(
                found=True,
                x=best_loc[0],
                y=best_loc[1],
                width=tw,
                height=th,
                confidence=float(best_val),
            )
        return DetectionResult(found=False, confidence=float(best_val))

    def find_from_file(
        self, haystack: np.ndarray, template_path: Path
    ) -> DetectionResult:
        template = cv2.imread(str(template_path))
        if template is None:
            logger.error(f"Template not found: {template_path}")
            return DetectionResult(found=False)
        return self.find(haystack, template)

    def find_all(
        self, haystack: np.ndarray, template: np.ndarray, max_count: int = 20
    ) -> list[DetectionResult]:
        """Find all occurrences of template."""
        h_gray = cv2.cvtColor(haystack, cv2.COLOR_BGR2GRAY) if len(haystack.shape) == 3 else haystack
        t_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY) if len(template.shape) == 3 else template
        result = cv2.matchTemplate(h_gray, t_gray, cv2.TM_CCOEFF_NORMED)
        th, tw = template.shape[:2]
        locs = np.where(result >= self._threshold)
        detections = []
        for y, x in zip(*locs):
            detections.append(DetectionResult(
                found=True,
                x=int(x), y=int(y),
                width=tw, height=th,
                confidence=float(result[y, x]),
            ))
            if len(detections) >= max_count:
                break
        return self._nms(detections, iou_threshold=0.3)

    @staticmethod
    def _nms(
        detections: list[DetectionResult], iou_threshold: float = 0.3
    ) -> list[DetectionResult]:
        """Non-maximum suppression to remove overlapping detections."""
        if not detections:
            return []
        detections.sort(key=lambda d: d.confidence, reverse=True)
        kept = []
        for d in detections:
            suppress = False
            for k in kept:
                ix1 = max(d.x, k.x)
                iy1 = max(d.y, k.y)
                ix2 = min(d.x + d.width, k.x + k.width)
                iy2 = min(d.y + d.height, k.y + k.height)
                inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
                area_d = d.width * d.height
                area_k = k.width * k.height
                iou = inter / (area_d + area_k - inter + 1e-6)
                if iou > iou_threshold:
                    suppress = True
                    break
            if not suppress:
                kept.append(d)
        return kept


class ChangeDetector:
    """Detects visual changes between two screenshots (before/after action)."""

    def __init__(self, min_area: int = 500, threshold: int = 30) -> None:
        self._min_area = min_area
        self._threshold = threshold

    def detect_changes(
        self, before: np.ndarray, after: np.ndarray
    ) -> list[DetectionResult]:
        """Return list of regions that changed between before and after."""
        if before is None or after is None:
            return []
        if before.shape != after.shape:
            after = cv2.resize(after, (before.shape[1], before.shape[0]))

        b_gray = cv2.cvtColor(before, cv2.COLOR_BGR2GRAY)
        a_gray = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)

        diff = cv2.absdiff(b_gray, a_gray)
        _, thresh = cv2.threshold(diff, self._threshold, 255, cv2.THRESH_BINARY)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        thresh = cv2.dilate(thresh, kernel, iterations=2)

        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        regions = []
        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < self._min_area:
                continue
            x, y, w, h = cv2.boundingRect(cnt)
            regions.append(DetectionResult(
                found=True,
                x=x, y=y, width=w, height=h,
                confidence=float(area) / (before.shape[0] * before.shape[1]),
            ))
        return regions

    def has_changed(self, before: np.ndarray, after: np.ndarray) -> bool:
        return bool(self.detect_changes(before, after))

    def similarity(self, img1: np.ndarray, img2: np.ndarray) -> float:
        """Structural similarity score [0, 1] between two images."""
        try:
            from skimage.metrics import structural_similarity as ssim
            if img1.shape != img2.shape:
                img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
            g1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
            g2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)
            score, _ = ssim(g1, g2, full=True)
            return float(score)
        except Exception:
            return 0.0


class ButtonDetector:
    """
    Detect UI buttons and controls using contour analysis.
    Used when UIA and OCR both fail to locate interactive elements.
    """

    def find_buttons(self, img: np.ndarray) -> list[DetectionResult]:
        """Find rectangular button-like regions."""
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if len(img.shape) == 3 else img
        edges = cv2.Canny(gray, 50, 150)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        buttons = []
        h_img, w_img = img.shape[:2]
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            aspect = w / max(h, 1)
            area = w * h
            # Typical button: wider than tall, reasonable size
            if 1.5 <= aspect <= 8.0 and 400 < area < 50000:
                # Check it's near the edge or has distinct border
                peri = cv2.arcLength(cnt, True)
                approx = cv2.approxPolyDP(cnt, 0.04 * peri, True)
                if 4 <= len(approx) <= 6:
                    buttons.append(DetectionResult(
                        found=True, x=x, y=y, width=w, height=h,
                        confidence=0.5,
                    ))
        return buttons


# Module-level singletons
_template_detector: TemplateDetector | None = None
_change_detector: ChangeDetector | None = None


def get_template_detector() -> "TemplateDetector":
    global _template_detector
    if _template_detector is None:
        if not CV2_AVAILABLE:
            raise RuntimeError("TemplateDetector requires cv2 (headless mode: not available)")
        _template_detector = TemplateDetector()
    return _template_detector


def get_change_detector() -> "ChangeDetector":
    global _change_detector
    if _change_detector is None:
        if not CV2_AVAILABLE:
            raise RuntimeError("ChangeDetector requires cv2 (headless mode: not available)")
        _change_detector = ChangeDetector()
    return _change_detector
