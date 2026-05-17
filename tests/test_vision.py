"""Tests for vision layer components."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import numpy as np
import pytest

from vision.screenshot_engine import Region, Screenshot
from vision.detector import TemplateDetector, ChangeDetector, DetectionResult
from vision.ocr_engine import TextBlock, OCRResult


# ─────────────────────────────────────────────────────────────────────────────
# Region tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRegion:
    def test_properties(self):
        r = Region(x=10, y=20, width=100, height=50)
        assert r.right == 110
        assert r.bottom == 70
        assert r.center == (60, 45)  # Actually (10 + 50, 20 + 25) = (60, 45)

    def test_as_mss_monitor(self):
        r = Region(x=0, y=0, width=1920, height=1080)
        m = r.as_mss_monitor()
        assert m["left"] == 0
        assert m["width"] == 1920

    def test_as_bbox(self):
        r = Region(x=10, y=20, width=100, height=50)
        assert r.as_bbox() == (10, 20, 110, 70)

    def test_with_padding(self):
        r = Region(x=50, y=50, width=100, height=100)
        padded = r.with_padding(10)
        assert padded.x == 40
        assert padded.y == 40
        assert padded.width == 120
        assert padded.height == 120

    def test_with_padding_clamps_negative(self):
        r = Region(x=5, y=5, width=100, height=100)
        padded = r.with_padding(10)
        assert padded.x == 0  # clamped at 0
        assert padded.y == 0


# ─────────────────────────────────────────────────────────────────────────────
# TemplateDetector tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTemplateDetector:
    def test_find_exact_match(self):
        detector = TemplateDetector(threshold=0.99)
        # Create a 200x200 image with a 50x50 white block at (75, 75)
        haystack = np.zeros((200, 200), dtype=np.uint8)
        haystack[75:125, 75:125] = 255
        template = haystack[75:125, 75:125].copy()
        result = detector.find(haystack, template)
        assert result.found
        assert result.x == 75
        assert result.y == 75

    def test_find_no_match(self):
        detector = TemplateDetector(threshold=0.99)
        haystack = np.zeros((100, 100), dtype=np.uint8)
        template = np.ones((50, 50), dtype=np.uint8) * 200
        result = detector.find(haystack, template)
        assert not result.found

    def test_template_larger_than_haystack(self):
        detector = TemplateDetector()
        haystack = np.zeros((10, 10), dtype=np.uint8)
        template = np.zeros((50, 50), dtype=np.uint8)
        result = detector.find(haystack, template)
        assert not result.found

    def test_none_inputs(self):
        detector = TemplateDetector()
        result = detector.find(None, None)
        assert not result.found

    def test_nms_removes_overlapping(self):
        detector = TemplateDetector()
        detections = [
            DetectionResult(found=True, x=0, y=0, width=50, height=50, confidence=0.9),
            DetectionResult(found=True, x=5, y=5, width=50, height=50, confidence=0.8),  # overlaps
            DetectionResult(found=True, x=200, y=200, width=50, height=50, confidence=0.7),  # distinct
        ]
        kept = detector._nms(detections, iou_threshold=0.3)
        assert len(kept) == 2


# ─────────────────────────────────────────────────────────────────────────────
# ChangeDetector tests
# ─────────────────────────────────────────────────────────────────────────────

class TestChangeDetector:
    def test_identical_images_no_change(self):
        detector = ChangeDetector()
        img = np.zeros((200, 200, 3), dtype=np.uint8)
        changes = detector.detect_changes(img, img.copy())
        assert len(changes) == 0

    def test_changed_region_detected(self):
        detector = ChangeDetector(min_area=100)
        before = np.zeros((300, 300, 3), dtype=np.uint8)
        after = before.copy()
        after[100:200, 100:200] = 200  # Large change
        changes = detector.detect_changes(before, after)
        assert len(changes) > 0

    def test_has_changed_true(self):
        detector = ChangeDetector(min_area=100)
        before = np.zeros((300, 300, 3), dtype=np.uint8)
        after = before.copy()
        after[50:250, 50:250] = 180
        assert detector.has_changed(before, after)

    def test_has_changed_false_identical(self):
        detector = ChangeDetector()
        img = np.ones((100, 100, 3), dtype=np.uint8) * 128
        assert not detector.has_changed(img, img.copy())

    def test_mismatched_sizes_handled(self):
        detector = ChangeDetector()
        before = np.zeros((100, 100, 3), dtype=np.uint8)
        after = np.zeros((150, 150, 3), dtype=np.uint8)
        # Should not raise
        detector.detect_changes(before, after)


# ─────────────────────────────────────────────────────────────────────────────
# OCRResult tests
# ─────────────────────────────────────────────────────────────────────────────

class TestOCRResult:
    def _make_result(self) -> OCRResult:
        blocks = [
            TextBlock("Сохранить", 0.9, 10, 10, 80, 20, "test"),
            TextBlock("Отмена", 0.85, 100, 10, 60, 20, "test"),
            TextBlock("Microsoft Word", 0.95, 200, 5, 120, 25, "test"),
        ]
        return OCRResult(blocks=blocks, full_text=" ".join(b.text for b in blocks))

    def test_find_text_exact(self):
        result = self._make_result()
        matches = result.find_text("Отмена")
        assert len(matches) == 1

    def test_find_text_case_insensitive(self):
        result = self._make_result()
        matches = result.find_text("сохранить", case_sensitive=False)
        assert len(matches) == 1

    def test_find_text_not_found(self):
        result = self._make_result()
        matches = result.find_text("НеСуществует")
        assert len(matches) == 0

    def test_best_match_exact(self):
        result = self._make_result()
        block = result.best_match("Сохранить", threshold=0.9)
        assert block is not None
        assert block.text == "Сохранить"

    def test_best_match_fuzzy(self):
        result = self._make_result()
        block = result.best_match("Сохран", threshold=0.5)
        assert block is not None

    def test_best_match_none_below_threshold(self):
        result = self._make_result()
        block = result.best_match("XYZ123", threshold=0.9)
        assert block is None

    def test_text_near_filters_by_radius(self):
        result = self._make_result()
        near = result.text_near(x=50, y=20, radius=60)
        texts = [b.text for b in near]
        assert "Сохранить" in texts
        assert "Microsoft Word" not in texts

    def test_text_block_center(self):
        block = TextBlock("OK", 0.9, 100, 200, 50, 20, "test")
        assert block.center == (125, 210)
