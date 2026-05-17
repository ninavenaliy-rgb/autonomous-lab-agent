"""
Task Planning Engine.
Converts ParsedTasks into validated execution plans.
Handles dynamic replanning when steps fail.
Uses LLM to clarify ambiguous instructions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from agents.parser_agent import ParsedTask, AtomicStep, LLMClient
from core.config import get_config


@dataclass
class ExecutionPlan:
    task_id: str
    steps: list[AtomicStep]
    metadata: dict = field(default_factory=dict)

    def get_step(self, index: int) -> AtomicStep | None:
        if 0 <= index < len(self.steps):
            return self.steps[index]
        return None

    def remaining_steps(self, from_index: int) -> list[AtomicStep]:
        return self.steps[from_index:]

    def total_steps(self) -> int:
        return len(self.steps)


# ─────────────────────────────────────────────────────────────────────────────
# Standard action templates for common patterns
# ─────────────────────────────────────────────────────────────────────────────

WORD_OPEN_TEMPLATE = [
    AtomicStep(step_id="s_pre_1", action="open_word", target="",
               description="Launch Microsoft Word", expected_outcome="Word is open"),
    AtomicStep(step_id="s_pre_2", action="focus_window", target="word",
               description="Focus Word window", expected_outcome="Word is in foreground"),
]

EXCEL_OPEN_TEMPLATE = [
    AtomicStep(step_id="s_pre_1", action="open_excel", target="",
               description="Launch Microsoft Excel", expected_outcome="Excel is open"),
    AtomicStep(step_id="s_pre_2", action="focus_window", target="excel",
               description="Focus Excel window", expected_outcome="Excel is in foreground"),
]

WORD_SAVE_TEMPLATE = [
    AtomicStep(step_id="s_post_1", action="save_file", target="",
               description="Save document", expected_outcome="File saved"),
]

SCREENSHOT_STEP = AtomicStep(
    step_id="s_screenshot", action="wait_ms", value="300",
    description="Wait for UI to stabilize before screenshot",
    expected_outcome="UI stable",
)


class Planner:
    """
    Converts high-level ParsedTasks into step-by-step ExecutionPlans.
    Enriches steps with pre/post conditions and recovery hints.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._llm = None
        try:
            self._llm = LLMClient()
        except Exception:
            pass

    def plan_task(self, task: ParsedTask, with_open_app: bool = True) -> ExecutionPlan:
        """Build a complete execution plan for a task."""
        steps: list[AtomicStep] = []

        # Pre-steps: ensure application is open
        if with_open_app and task.application in ("word", "excel"):
            if task.application == "word":
                steps.extend(WORD_OPEN_TEMPLATE)
            else:
                steps.extend(EXCEL_OPEN_TEMPLATE)

        # Main task steps
        if task.steps:
            validated = [self._validate_step(s, task) for s in task.steps]
            steps.extend(validated)
        else:
            # Generate steps from description using LLM or heuristics
            generated = self._generate_steps(task)
            steps.extend(generated)

        # Post-steps: save and screenshot
        if task.application == "word":
            steps.extend(WORD_SAVE_TEMPLATE)
        elif task.application == "excel":
            steps.append(AtomicStep(
                step_id="s_post_1", action="save_file",
                description="Save workbook", expected_outcome="File saved"
            ))

        # Screenshot step at end
        steps.append(AtomicStep(
            step_id="s_screenshot_final",
            action="wait_ms", value="500",
            description="Wait before final screenshot",
            expected_outcome="",
        ))

        # Renumber step IDs
        for i, step in enumerate(steps):
            step.step_id = f"s{i+1}"

        plan = ExecutionPlan(task_id=task.task_id, steps=steps)
        logger.debug(
            f"Plan built: task={task.task_id} steps={plan.total_steps()}"
        )
        return plan

    def replan_from_failure(
        self,
        task: ParsedTask,
        plan: ExecutionPlan,
        failed_step_index: int,
        error: str = "",
    ) -> ExecutionPlan:
        """Generate alternative steps when a step fails repeatedly."""
        failed_step = plan.get_step(failed_step_index)
        if not failed_step:
            return plan

        logger.info(f"Replanning from failure at step {failed_step_index}: {failed_step.action}")

        # Strategy: use alternative approaches
        alternatives = self._get_alternatives(failed_step, error)
        if not alternatives:
            return plan

        # Replace the failed step with alternatives
        new_steps = (
            plan.steps[:failed_step_index]
            + alternatives
            + plan.steps[failed_step_index + 1:]
        )
        for i, step in enumerate(new_steps):
            step.step_id = f"s{i+1}"

        return ExecutionPlan(
            task_id=task.task_id,
            steps=new_steps,
            metadata={"replanned_at": failed_step_index, "reason": error},
        )

    def _get_alternatives(self, step: AtomicStep, error: str) -> list[AtomicStep]:
        """Get alternative steps for a failed step."""
        action = step.action

        alternatives_map: dict[str, list[AtomicStep]] = {
            "click": [
                AtomicStep(
                    step_id="alt_1",
                    action="hotkey",
                    value="enter",
                    description=f"Press Enter instead of clicking {step.target}",
                    expected_outcome=step.expected_outcome,
                )
            ],
            "menu_navigate": [
                AtomicStep(
                    step_id="alt_1",
                    action="hotkey",
                    value=self._infer_shortcut(step.target),
                    description=f"Use keyboard shortcut for {step.target}",
                    expected_outcome=step.expected_outcome,
                )
            ],
            "type": [
                AtomicStep(
                    step_id="alt_1",
                    action="click",
                    target=step.target,
                    description=f"Re-focus before typing",
                    expected_outcome="Element focused",
                ),
                AtomicStep(
                    step_id="alt_2",
                    action="type",
                    target=step.target,
                    value=step.value,
                    description=f"Retry typing {step.value[:30]!r}",
                    expected_outcome=step.expected_outcome,
                ),
            ],
        }
        return alternatives_map.get(action, [])

    def _infer_shortcut(self, menu_path: str) -> str:
        """Map common menu paths to keyboard shortcuts."""
        shortcuts = {
            "файл/сохранить": "ctrl+s",
            "файл/сохранить как": "ctrl+shift+s",
            "файл/открыть": "ctrl+o",
            "правка/копировать": "ctrl+c",
            "правка/вставить": "ctrl+v",
            "правка/отменить": "ctrl+z",
            "вставка/рисунок": "alt+n+p",
            "вставка/таблица": "alt+n+t",
            "формат/шрифт": "ctrl+d",
            "file/save": "ctrl+s",
            "file/open": "ctrl+o",
        }
        path_lower = menu_path.lower().replace(" ", "").replace("→", "/")
        for key, shortcut in shortcuts.items():
            if key in path_lower:
                return shortcut
        return "escape"

    def _validate_step(self, step: AtomicStep, task: ParsedTask) -> AtomicStep:
        """Validate and enrich a step with defaults."""
        # Ensure step has an action
        if not step.action:
            step.action = "wait_ms"
            step.value = "100"
        # Ensure step has description
        if not step.description:
            step.description = f"{step.action} {step.target}"
        return step

    def _generate_steps(self, task: ParsedTask) -> list[AtomicStep]:
        """Generate steps from task description when none are provided."""
        if self._llm and self._llm._client:
            return self._generate_steps_llm(task)
        return self._generate_steps_heuristic(task)

    def _generate_steps_llm(self, task: ParsedTask) -> list[AtomicStep]:
        system = """Generate atomic GUI automation steps for this task.
Return JSON array of step objects. Each step must have:
{
  "step_id": "s1",
  "action": "click|type|hotkey|menu_navigate|...",
  "target": "element name or empty",
  "value": "text or shortcut or empty",
  "description": "what this step does",
  "expected_outcome": "what should happen",
  "optional": false
}

Available actions: open_word, open_excel, open_file, save_file, click, type,
hotkey, menu_navigate, format_text, apply_style, insert_table, insert_image,
select_text, scroll, enter_formula, click_cell, create_chart, wait_ms.

Return ONLY the JSON array, no prose."""

        user = f"""Task: {task.title}
Application: {task.application}
Description: {task.description}
Expected: {task.expected_result}"""

        try:
            response = self._llm.complete(system, user)
            import re
            match = re.search(r"\[.*\]", response, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                return [AtomicStep(**s) for s in data]
        except Exception as exc:
            logger.debug(f"LLM step generation failed: {exc}")
        return self._generate_steps_heuristic(task)

    def _generate_steps_heuristic(self, task: ParsedTask) -> list[AtomicStep]:
        """Simple heuristic step generation as final fallback."""
        desc_lower = task.description.lower()
        steps = []

        if "таблиц" in desc_lower or "table" in desc_lower:
            steps.append(AtomicStep(
                step_id="h1", action="insert_table",
                value="3", description="Insert table",
                expected_outcome="Table inserted",
            ))
        if "заголовок" in desc_lower or "heading" in desc_lower:
            steps.append(AtomicStep(
                step_id="h2", action="apply_style",
                value="Заголовок 1", description="Apply Heading 1 style",
                expected_outcome="Style applied",
            ))
        if "шрифт" in desc_lower or "font" in desc_lower:
            steps.append(AtomicStep(
                step_id="h3", action="set_font",
                value="Times New Roman", description="Set font",
                expected_outcome="Font changed",
            ))
        if "изображени" in desc_lower or "скриншот" in desc_lower or "рисун" in desc_lower:
            steps.append(AtomicStep(
                step_id="h4", action="insert_image",
                target="", description="Insert image",
                expected_outcome="Image inserted",
            ))
        if "сохран" in desc_lower or "save" in desc_lower:
            steps.append(AtomicStep(
                step_id="h5", action="save_file",
                description="Save file", expected_outcome="Saved",
            ))

        if not steps:
            # Generic "perform task" step
            steps.append(AtomicStep(
                step_id="h1",
                action="wait_ms",
                value="1000",
                description=f"Perform: {task.description[:80]}",
                expected_outcome=task.expected_result,
            ))
        return steps


# Module-level singleton
_planner: Planner | None = None


def get_planner() -> Planner:
    global _planner
    if _planner is None:
        _planner = Planner()
    return _planner
