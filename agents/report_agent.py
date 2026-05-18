"""
Report Agent — assembles the final DOCX laboratory report.
Pulls screenshots, task results, and LLM-generated descriptions
into a properly formatted report with captions, TOC, and figures.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from agents.parser_agent import ParsedTask, ParsedMethodology
from core.config import get_config
from report.docx_builder import DocxBuilder, ReportSection, ReportMeta, get_docx_builder
from report.caption_manager import get_caption_manager
from storage.database import DatabaseManager
from vision.screenshot_engine import Screenshot


@dataclass
class TaskResult:
    task_id: str
    title: str
    description: str
    screenshots: list[Screenshot] = field(default_factory=list)
    screenshot_captions: list[str] = field(default_factory=list)
    completed: bool = True
    error: str = ""
    generated_description: str = ""
    tables: list[list[list[str]]] = field(default_factory=list)
    table_captions: list[str] = field(default_factory=list)


class ReportAgent:
    """
    Builds the final DOCX report from collected task results.
    Uses LLM to generate descriptive captions and conclusions.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._cfg = cfg
        self._builder = get_docx_builder()
        self._captions = get_caption_manager()
        self._llm = None
        self._reference_text: str = ""
        self._init_llm()

    def set_reference(self, text: str) -> None:
        self._reference_text = text

    def _init_llm(self) -> None:
        try:
            from agents.parser_agent import LLMClient
            self._llm = LLMClient()
        except Exception as exc:
            logger.warning(f"LLM client not available for report: {exc}")

    def build_report(
        self,
        methodology: ParsedMethodology,
        task_results: list[TaskResult],
        meta: ReportMeta | None = None,
        output_path: Path | None = None,
    ) -> Path:
        """
        Assemble and save the complete lab report.
        Returns path to generated DOCX.
        """
        t0 = time.time()
        logger.info(f"Building report for: {methodology.title}")

        cfg = self._cfg
        output = output_path or (
            cfg.storage.report_output_dir
            / f"lab_report_{int(t0)}.docx"
        )

        self._builder.new_document()

        # Title page
        if meta is None:
            meta = ReportMeta(
                title=methodology.title,
                university=cfg.report.university,
                department=cfg.report.department,
            )
        self._builder.add_title_page(meta)

        # Table of Contents field
        self._builder.add_toc()

        # Introduction section
        if methodology.global_context:
            intro = ReportSection(
                title="Введение",
                level=1,
                content=methodology.global_context,
            )
            self._builder.add_section(intro)
            self._builder.add_page_break()

        # Task sections
        for result in task_results:
            section = self._build_section(result)
            self._builder.add_section(section)

        # Conclusion
        conclusion_text = self._generate_conclusion(methodology, task_results)
        self._builder.add_conclusion(conclusion_text)

        # List of figures (if any)
        all_figs = self._captions.all_figures()
        if all_figs:
            self._builder.add_page_break()
            self._builder.add_paragraph("СПИСОК РИСУНКОВ", bold=True, align="center")
            for fig in all_figs:
                self._builder.add_paragraph(fig.caption)

        # Save
        saved_path = self._builder.save(output)
        duration = time.time() - t0
        logger.info(f"Report generated in {duration:.1f}s: {saved_path}")

        # Try to update TOC via COM automation
        try:
            from report.toc_manager import TOCManager
            toc = TOCManager()
            toc.update_toc_in_document(str(saved_path))
        except Exception:
            pass

        return saved_path

    def _build_section(self, result: TaskResult) -> ReportSection:
        """Convert a TaskResult into a report section."""
        # Generate descriptive content via LLM if available
        content = result.generated_description or result.description
        if not content and self._llm and self._llm._client:
            content = self._generate_section_description(result)

        # Build screenshot caption list
        fig_paths = [str(s.path) for s in result.screenshots]
        fig_captions = result.screenshot_captions or [
            self._generate_caption(result.title, i + 1)
            for i in range(len(result.screenshots))
        ]
        # Pad captions if needed
        while len(fig_captions) < len(fig_paths):
            fig_captions.append(f"Результат выполнения задания {result.title}")

        return ReportSection(
            title=result.title,
            level=2,
            content=content,
            figures=fig_paths,
            figure_captions=fig_captions,
            tables=result.tables,
            table_captions=result.table_captions,
            task_id=result.task_id,
        )

    def _generate_section_description(self, result: TaskResult) -> str:
        """Use LLM to generate a paragraph description of what was done."""
        if not self._llm:
            return result.description

        ref_block = ""
        if self._reference_text:
            ref_block = (
                f"\n\nОБРАЗЕЦ СТИЛЯ (пиши похожим языком и с такой же детальностью):\n"
                f"{self._reference_text[:1500]}\n"
            )

        system = f"""You are writing a laboratory report in Russian.
Given a task description, write 2-3 clear academic sentences describing:
1. What was performed
2. What the result was
3. What was learned
Write in past tense, in Russian. No lists — flowing prose only.{ref_block}"""

        user = f"""Task: {result.title}
Description: {result.description}
Status: {"Completed" if result.completed else "Failed: " + result.error}
Screenshots taken: {len(result.screenshots)}

Write the description paragraph in Russian."""

        try:
            return self._llm.complete(system, user)
        except Exception as exc:
            logger.debug(f"LLM section generation failed: {exc}")
            return result.description or ""

    def _generate_caption(self, task_title: str, fig_num: int) -> str:
        """Generate a descriptive figure caption."""
        return f"Результат выполнения задания «{task_title}»"

    def _generate_conclusion(
        self,
        methodology: ParsedMethodology,
        results: list[TaskResult],
    ) -> str:
        completed = sum(1 for r in results if r.completed)
        total = len(results)

        if self._llm and self._llm._client:
            system = """Write a conclusion for a student laboratory report in Russian.
The conclusion should summarize what skills were practiced, what was accomplished,
and what knowledge was gained. 3-5 sentences. Academic style."""
            user = f"""Lab: {methodology.title}
Completed: {completed}/{total} tasks
Tasks: {', '.join(r.title for r in results[:5])}
Write the conclusion in Russian."""
            try:
                return self._llm.complete(system, user)
            except Exception:
                pass

        # Fallback
        return (
            f"В ходе выполнения лабораторной работы «{methodology.title}» "
            f"было выполнено {completed} из {total} заданий. "
            f"Получены практические навыки работы с программным обеспечением Microsoft Office."
        )

    def add_task_result(
        self,
        task: ParsedTask,
        screenshots: list[Screenshot],
        completed: bool = True,
        error: str = "",
        generated_desc: str = "",
    ) -> TaskResult:
        """Factory method to create a TaskResult."""
        captions = [
            f"Скриншот {i+1}: {task.title}"
            for i in range(len(screenshots))
        ]
        return TaskResult(
            task_id=task.task_id,
            title=task.title,
            description=task.description,
            screenshots=screenshots,
            screenshot_captions=captions,
            completed=completed,
            error=error,
            generated_description=generated_desc,
        )


# Module-level singleton
_agent: ReportAgent | None = None


def get_report_agent() -> ReportAgent:
    global _agent
    if _agent is None:
        _agent = ReportAgent()
    return _agent
