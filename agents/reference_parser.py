"""
Parses a reference (sample) completed lab report and extracts its structure.
Used to guide the agent when generating a new report in the same style.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

try:
    from docx import Document as DocxDocument
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    import pdfplumber
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False


@dataclass
class ReferenceDoc:
    headings: list[str] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)
    full_text: str = ""
    # Compact version sent to LLM (first 4000 chars + heading list)
    llm_context: str = ""


def parse_reference(path: Path) -> ReferenceDoc | None:
    """Extract text and structure from a reference docx/pdf. Returns None on failure."""
    try:
        suffix = path.suffix.lower()
        if suffix == ".docx":
            return _from_docx(path)
        elif suffix == ".pdf":
            return _from_pdf(path)
        else:
            logger.warning(f"Unsupported reference format: {suffix}")
            return None
    except Exception as exc:
        logger.warning(f"Reference parse failed: {exc}")
        return None


def _from_docx(path: Path) -> ReferenceDoc:
    if not DOCX_AVAILABLE:
        raise RuntimeError("python-docx not installed")
    doc = DocxDocument(str(path))
    headings: list[str] = []
    sections: dict[str, str] = {}
    current_heading = ""
    current_lines: list[str] = []
    all_lines: list[str] = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        all_lines.append(text)
        style = (para.style.name or "").lower()
        if "heading" in style or "заголовок" in style:
            if current_heading and current_lines:
                sections[current_heading] = "\n".join(current_lines)
            current_heading = text
            current_lines = []
            headings.append(text)
        else:
            current_lines.append(text)

    if current_heading and current_lines:
        sections[current_heading] = "\n".join(current_lines)

    full_text = "\n".join(all_lines)
    llm_context = _build_llm_context(headings, sections, full_text)
    return ReferenceDoc(
        headings=headings,
        sections=sections,
        full_text=full_text,
        llm_context=llm_context,
    )


def _from_pdf(path: Path) -> ReferenceDoc:
    if not PDF_AVAILABLE:
        raise RuntimeError("pdfplumber not installed")
    lines: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(line.strip() for line in text.splitlines() if line.strip())
    full_text = "\n".join(lines)
    llm_context = _build_llm_context([], {}, full_text)
    return ReferenceDoc(full_text=full_text, llm_context=llm_context)


def _build_llm_context(
    headings: list[str],
    sections: dict[str, str],
    full_text: str,
    max_chars: int = 5000,
) -> str:
    parts = []
    if headings:
        parts.append("СТРУКТУРА ОБРАЗЦА:\n" + "\n".join(f"  - {h}" for h in headings))
    # Include first section as style example
    for heading, content in list(sections.items())[:2]:
        parts.append(f"\nПРИМЕР РАЗДЕЛА «{heading}»:\n{content[:800]}")
    # And the very start of the document
    parts.append(f"\nНАЧАЛО ДОКУМЕНТА:\n{full_text[:1500]}")
    ctx = "\n".join(parts)
    return ctx[:max_chars]
