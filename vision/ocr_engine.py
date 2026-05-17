"""
OCR pipeline: EasyOCR primary, Tesseract fallback.
Provides text extraction from screenshots with confidence scores.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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

from core.config import get_config, OCRBackend


@dataclass
class TextBlock:
    text: str
    confidence: float
    x: int
    y: int
    width: int
    height: int
    source: str = "unknown"  # easyocr / tesseract

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.width, self.y + self.height)


@dataclass
class OCRResult:
    blocks: list[TextBlock] = field(default_factory=list)
    full_text: str = ""
    source: str = "none"

    def find_text(self, pattern: str, case_sensitive: bool = False) -> list[TextBlock]:
        flags = 0 if case_sensitive else re.IGNORECASE
        return [b for b in self.blocks if re.search(pattern, b.text, flags)]

    def text_near(self, x: int, y: int, radius: int = 100) -> list[TextBlock]:
        result = []
        for b in self.blocks:
            cx, cy = b.center
            if abs(cx - x) <= radius and abs(cy - y) <= radius:
                result.append(b)
        return result

    def best_match(self, target: str, threshold: float = 0.6) -> TextBlock | None:
        """Fuzzy find closest text block matching target string."""
        from difflib import SequenceMatcher
        best_ratio = 0.0
        best_block = None
        for b in self.blocks:
            ratio = SequenceMatcher(None, target.lower(), b.text.lower()).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_block = b
        if best_ratio >= threshold:
            return best_block
        return None


class EasyOCREngine:
    _instance = None

    def __init__(self, languages: list[str]) -> None:
        try:
            import easyocr
            self._reader = easyocr.Reader(languages, gpu=False, verbose=False)
            self._available = True
            logger.info(f"EasyOCR initialized with languages: {languages}")
        except ImportError:
            self._available = False
            logger.warning("EasyOCR not available")

    def extract(self, img: np.ndarray, threshold: float = 0.5) -> list[TextBlock]:
        if not self._available:
            return []
        try:
            results = self._reader.readtext(img, detail=1, paragraph=False)
            blocks = []
            for bbox_pts, text, conf in results:
                if conf < threshold or not text.strip():
                    continue
                pts = np.array(bbox_pts, dtype=int)
                x = int(pts[:, 0].min())
                y = int(pts[:, 1].min())
                w = int(pts[:, 0].max()) - x
                h = int(pts[:, 1].max()) - y
                blocks.append(TextBlock(
                    text=text.strip(),
                    confidence=float(conf),
                    x=x, y=y, width=w, height=h,
                    source="easyocr",
                ))
            return blocks
        except Exception as exc:
            logger.error(f"EasyOCR extraction failed: {exc}")
            return []


class TesseractEngine:
    def __init__(self, cmd: str, languages: list[str]) -> None:
        self._available = False
        try:
            import pytesseract
            pytesseract.pytesseract.tesseract_cmd = cmd
            pytesseract.get_tesseract_version()
            self._pytesseract = pytesseract
            self._lang_str = "+".join(
                self._map_lang(l) for l in languages
            )
            self._available = True
            logger.info(f"Tesseract initialized: lang={self._lang_str}")
        except Exception as exc:
            logger.warning(f"Tesseract not available: {exc}")

    @staticmethod
    def _map_lang(lang: str) -> str:
        mapping = {"ru": "rus", "en": "eng", "de": "deu", "fr": "fra"}
        return mapping.get(lang.lower(), lang)

    def extract(self, img: np.ndarray, threshold: float = 0.5) -> list[TextBlock]:
        if not self._available:
            return []
        try:
            from PIL import Image as PILImage
            pil = PILImage.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            data = self._pytesseract.image_to_data(
                pil,
                lang=self._lang_str,
                output_type=self._pytesseract.Output.DICT,
                config="--psm 11",
            )
            blocks = []
            for i in range(len(data["text"])):
                text = str(data["text"][i]).strip()
                conf_raw = data["conf"][i]
                if not text or conf_raw == -1:
                    continue
                conf = float(conf_raw) / 100.0
                if conf < threshold:
                    continue
                blocks.append(TextBlock(
                    text=text,
                    confidence=conf,
                    x=data["left"][i],
                    y=data["top"][i],
                    width=data["width"][i],
                    height=data["height"][i],
                    source="tesseract",
                ))
            return blocks
        except Exception as exc:
            logger.error(f"Tesseract extraction failed: {exc}")
            return []


class OCREngine:
    """
    Unified OCR pipeline. EasyOCR is primary; Tesseract is fallback.
    Preprocesses images for best accuracy with Russian text.
    """

    def __init__(self) -> None:
        cfg = get_config()
        langs = cfg.vision.ocr_languages
        self._threshold = cfg.vision.confidence_threshold
        self._backend = cfg.vision.ocr_backend
        self._easy = EasyOCREngine(langs)
        self._tess = TesseractEngine(cfg.vision.tesseract_cmd, langs)

    def _preprocess(self, img: np.ndarray) -> np.ndarray:
        """Enhance image for OCR: grayscale, denoise, threshold."""
        if img is None or img.size == 0:
            return img
        if len(img.shape) == 3:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        else:
            gray = img.copy()
        # Upscale small images
        h, w = gray.shape[:2]
        if w < 600:
            scale = 600 / w
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        # Denoise
        gray = cv2.fastNlMeansDenoising(gray, h=10)
        # Adaptive threshold for better contrast
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )
        return binary

    def extract_from_array(
        self, img: np.ndarray, preprocess: bool = True
    ) -> OCRResult:
        if img is None or img.size == 0:
            return OCRResult()

        proc = self._preprocess(img) if preprocess else img

        blocks: list[TextBlock] = []
        source = "none"

        if self._backend in (OCRBackend.EASYOCR, OCRBackend.BOTH):
            blocks = self._easy.extract(proc, self._threshold)
            if blocks:
                source = "easyocr"

        if not blocks or self._backend == OCRBackend.BOTH:
            tess_blocks = self._tess.extract(proc, self._threshold)
            if not blocks:
                blocks = tess_blocks
                source = "tesseract"
            elif tess_blocks:
                # Merge: prefer EasyOCR but fill gaps with Tesseract
                easy_texts = {b.text.lower() for b in blocks}
                for tb in tess_blocks:
                    if tb.text.lower() not in easy_texts:
                        blocks.append(tb)
                source = "merged"

        full_text = " ".join(b.text for b in blocks)
        return OCRResult(blocks=blocks, full_text=full_text, source=source)

    def extract_from_file(self, path: Path, preprocess: bool = True) -> OCRResult:
        img = cv2.imread(str(path))
        if img is None:
            logger.error(f"Cannot read image: {path}")
            return OCRResult()
        return self.extract_from_array(img, preprocess)

    def find_text_in_image(
        self, img: np.ndarray, target: str, threshold: float = 0.6
    ) -> TextBlock | None:
        result = self.extract_from_array(img)
        return result.best_match(target, threshold)

    def contains_text(self, img: np.ndarray, pattern: str) -> bool:
        result = self.extract_from_array(img)
        return bool(result.find_text(pattern))


# Module-level singleton
_ocr: OCREngine | None = None


def get_ocr_engine() -> "OCREngine":
    global _ocr
    if _ocr is None:
        if not CV2_AVAILABLE:
            raise RuntimeError("OCREngine requires cv2 (headless mode: not available)")
        _ocr = OCREngine()
    return _ocr
