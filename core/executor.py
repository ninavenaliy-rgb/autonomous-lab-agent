"""
Action Execution Engine.
Executes individual AtomicSteps with retry, timeout, validation, and logging.
Bridges the Planner with the GUIAgent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from loguru import logger

import platform

IS_WINDOWS = platform.system() == "Windows"

from agents.gui_agent import ActionResult, ActionStatus
from agents.parser_agent import AtomicStep
from core.config import get_config
from core.planner import ExecutionPlan
from vision.screenshot_engine import Screenshot

if IS_WINDOWS:
    from agents.gui_agent import get_gui_agent
    from agents.qa_agent import get_qa_agent
    from agents.recovery_agent import get_recovery_agent
    from recovery.watchdog import OperationTimer
    from vision.screenshot_engine import get_screenshot_engine


@dataclass
class StepExecutionResult:
    step: AtomicStep
    action_result: ActionResult
    validation_passed: bool = True
    screenshot: Screenshot | None = None
    replanned: bool = False
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.action_result.ok and self.validation_passed


@dataclass
class PlanExecutionResult:
    task_id: str
    completed: bool
    steps_executed: int
    steps_succeeded: int
    steps_failed: int
    duration_seconds: float
    screenshots: list[Screenshot] = field(default_factory=list)
    error: str = ""

    @property
    def success_rate(self) -> float:
        if self.steps_executed == 0:
            return 1.0
        return self.steps_succeeded / self.steps_executed


class Executor:
    """
    Executes ExecutionPlans step by step.
    Handles per-step timeouts, validation, screenshots, and recovery triggers.
    """

    def __init__(self) -> None:
        cfg = get_config()
        self._cfg = cfg
        self._step_timeout = cfg.ui.wait_timeout_seconds

        if IS_WINDOWS:
            # Full GUI automation mode
            self._gui = get_gui_agent()
            self._qa = get_qa_agent()
            self._recovery = get_recovery_agent()
            self._screen = get_screenshot_engine()
            self._headless = False
            logger.info("Executor: GUI mode (Windows)")
        else:
            # Headless mode — python-docx/openpyxl direct API
            from core.document_executor import HeadlessExecutor
            self._gui = HeadlessExecutor()
            self._qa = None
            self._recovery = None
            self._screen = None
            self._headless = True
            logger.info("Executor: headless mode (no GUI)")

        # Callbacks
        self._on_step_complete: Callable[[StepExecutionResult], None] | None = None
        self._on_step_failed: Callable[[StepExecutionResult], None] | None = None
        self._heartbeat_fn: Callable[[], None] | None = None

    def set_step_callbacks(
        self,
        on_complete: Callable[[StepExecutionResult], None] | None = None,
        on_failed: Callable[[StepExecutionResult], None] | None = None,
    ) -> None:
        self._on_step_complete = on_complete
        self._on_step_failed = on_failed

    def set_heartbeat(self, fn: Callable[[], None]) -> None:
        self._heartbeat_fn = fn

    def execute_plan(
        self,
        plan: ExecutionPlan,
        start_from: int = 0,
        context: dict | None = None,
    ) -> PlanExecutionResult:
        """Execute all steps in a plan from start_from index."""
        ctx = context or {}
        t0 = time.time()
        screenshots: list[Screenshot] = []
        steps_executed = 0
        steps_succeeded = 0
        steps_failed = 0
        last_error = ""

        remaining = plan.remaining_steps(start_from)
        logger.info(
            f"Executing plan: task={plan.task_id} "
            f"steps={len(remaining)} (from={start_from})"
        )

        for step in remaining:
            if self._heartbeat_fn:
                self._heartbeat_fn()

            # Capture before screenshot for change detection (Windows only)
            before_shot = self._screen.capture_full("before") if self._screen else None

            # Execute with timeout
            result = self._execute_step_with_timeout(step, before_shot, ctx)
            steps_executed += 1

            if result.ok:
                steps_succeeded += 1
                if result.screenshot:
                    screenshots.append(result.screenshot)
                if self._on_step_complete:
                    self._on_step_complete(result)
                if self._recovery:
                    self._recovery.record_success()
            elif step.optional:
                logger.info(f"Optional step skipped/failed: {step.step_id}")
                steps_succeeded += 1  # Optional failures don't count
            else:
                steps_failed += 1
                last_error = result.action_result.error
                if self._on_step_failed:
                    self._on_step_failed(result)
                opened = self._recovery.record_failure(f"{plan.task_id}:{step.step_id}") if self._recovery else False
                if opened:
                    logger.error("Circuit breaker opened — stopping plan execution")
                    break
                # Continue with next step unless it's critical
                if self._is_critical_step(step):
                    logger.error(f"Critical step failed: {step.step_id}")
                    break

        duration = time.time() - t0
        completed = steps_failed == 0
        result = PlanExecutionResult(
            task_id=plan.task_id,
            completed=completed,
            steps_executed=steps_executed,
            steps_succeeded=steps_succeeded,
            steps_failed=steps_failed,
            duration_seconds=duration,
            screenshots=screenshots,
            error=last_error,
        )
        logger.info(
            f"Plan done: task={plan.task_id} "
            f"ok={completed} "
            f"{steps_succeeded}/{steps_executed} steps "
            f"in {duration:.1f}s"
        )
        return result

    def _execute_step_with_timeout(
        self,
        step: AtomicStep,
        before_shot: Screenshot | None,
        context: dict,
    ) -> StepExecutionResult:
        """Execute a single step with optional timeout guard (Windows only)."""
        if self._headless:
            return self._execute_step(step, before_shot, context)
        try:
            with OperationTimer(self._step_timeout, step.action):
                return self._execute_step(step, before_shot, context)
        except TimeoutError:
            logger.error(f"Step timeout: {step.step_id} ({step.action})")
            fail_shot = self._screen.capture_failure(f"timeout_{step.step_id}") if self._screen else None
            return StepExecutionResult(
                step=step,
                action_result=ActionResult(
                    status=ActionStatus.FAILED,
                    action=step.action,
                    target=step.target,
                    error="Timeout",
                ),
                screenshot=fail_shot,
            )

    def _execute_step(
        self,
        step: AtomicStep,
        before_shot: Screenshot | None,
        context: dict,
    ) -> StepExecutionResult:
        """Execute a single step and validate the outcome."""
        logger.debug(
            f"Step {step.step_id}: {step.action} "
            f"target={step.target!r} value={str(step.value)[:40]!r}"
        )

        # Build kwargs from step
        kwargs = {
            "target": step.target,
            "value": step.value,
        }
        # Add context values
        if context.get("application"):
            kwargs["application"] = context["application"]
        if context.get("file_path"):
            kwargs.setdefault("file_path", context["file_path"])

        # Execute via GUI agent
        t0 = time.time()
        action_result = self._gui.execute(step.action, **kwargs)
        duration_ms = (time.time() - t0) * 1000

        # Capture after screenshot (Windows GUI or Mac headless Word)
        after_shot: Screenshot | None = None
        if action_result.ok and self._screen:
            after_shot = self._screen.capture_full(f"after_{step.step_id}")
        elif action_result.ok:
            shot_path = getattr(action_result, "_screenshot_path", None)
            if shot_path and Path(shot_path).exists():
                after_shot = Screenshot(
                    path=Path(shot_path),
                    width=0,
                    height=0,
                    region=None,
                    taken_at=time.time(),
                    caption=f"Выполнение шага: {step.action}",
                )

        # Validate outcome (Windows only)
        validation_passed = True
        if step.expected_outcome and action_result.ok and self._qa:
            vr = self._qa.validate_step_action(
                step.action,
                step.target,
                step.expected_outcome,
                before_shot,
            )
            validation_passed = vr.passed
            if not validation_passed:
                logger.debug(
                    f"Step validation failed: {vr.description}"
                )

        return StepExecutionResult(
            step=step,
            action_result=action_result,
            validation_passed=validation_passed,
            screenshot=after_shot,
        )

    def _is_critical_step(self, step: AtomicStep) -> bool:
        """Determine if a step failure should abort the plan."""
        critical_actions = {"open_word", "open_excel", "open_file"}
        return step.action in critical_actions and not step.optional

    def execute_single(
        self,
        action: str,
        target: str = "",
        value: str = "",
        **kwargs,
    ) -> ActionResult:
        """Execute a single ad-hoc action outside of a plan."""
        return self._gui.execute(action, target=target, value=value, **kwargs)


# Module-level singleton
_executor: Executor | None = None


def get_executor() -> Executor:
    global _executor
    if _executor is None:
        _executor = Executor()
    return _executor
