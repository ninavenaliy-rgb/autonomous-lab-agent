"""
Caption and figure/table numbering manager.
Maintains sequential numbering across the entire report.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger


@dataclass
class CaptionEntry:
    number: int
    category: str       # "figure" | "table" | "equation"
    label: str          # e.g. "Рисунок 1" or "Таблица 1"
    caption: str        # Full caption text
    file_path: str = "" # For figures: path to image file
    anchor: str = ""    # Bookmark anchor name in DOCX


class CaptionManager:
    """
    Manages sequential numbering of figures, tables, and equations.
    Thread-safe counter per category.
    Generates ГОСТ-style caption strings.
    """

    def __init__(
        self,
        figure_prefix: str = "Рисунок",
        table_prefix: str = "Таблица",
        equation_prefix: str = "Формула",
    ) -> None:
        self._counters: dict[str, int] = {
            "figure": 0,
            "table": 0,
            "equation": 0,
        }
        self._prefixes = {
            "figure": figure_prefix,
            "table": table_prefix,
            "equation": equation_prefix,
        }
        self._registry: dict[str, CaptionEntry] = {}  # key = label like "Рисунок 1"

    def next_figure(self, caption: str, file_path: str = "") -> CaptionEntry:
        return self._next("figure", caption, file_path)

    def next_table(self, caption: str) -> CaptionEntry:
        return self._next("table", caption)

    def next_equation(self, caption: str) -> CaptionEntry:
        return self._next("equation", caption)

    def _next(self, category: str, caption: str, file_path: str = "") -> CaptionEntry:
        self._counters[category] += 1
        num = self._counters[category]
        prefix = self._prefixes[category]
        label = f"{prefix} {num}"
        anchor = f"cap_{category}_{num}"
        full_caption = f"{label} — {caption}" if caption else label

        entry = CaptionEntry(
            number=num,
            category=category,
            label=label,
            caption=full_caption,
            file_path=file_path,
            anchor=anchor,
        )
        self._registry[label] = entry
        logger.debug(f"Caption registered: {full_caption}")
        return entry

    def get(self, label: str) -> CaptionEntry | None:
        return self._registry.get(label)

    def all_figures(self) -> list[CaptionEntry]:
        return [e for e in self._registry.values() if e.category == "figure"]

    def all_tables(self) -> list[CaptionEntry]:
        return [e for e in self._registry.values() if e.category == "table"]

    def reset(self) -> None:
        for k in self._counters:
            self._counters[k] = 0
        self._registry.clear()

    def current_figure_number(self) -> int:
        return self._counters["figure"]

    def current_table_number(self) -> int:
        return self._counters["table"]

    def generate_list_of_figures(self) -> str:
        """Generate a formatted list of figures for report appendix."""
        lines = []
        for entry in self.all_figures():
            lines.append(f"{entry.label}\t{entry.caption}")
        return "\n".join(lines)

    def generate_list_of_tables(self) -> str:
        lines = []
        for entry in self.all_tables():
            lines.append(f"{entry.label}\t{entry.caption}")
        return "\n".join(lines)


# Module-level singleton
_manager: CaptionManager | None = None


def get_caption_manager() -> CaptionManager:
    global _manager
    if _manager is None:
        _manager = CaptionManager()
    return _manager
