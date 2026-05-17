"""
Central Orchestrator.
Coordinates all agents and subsystems through the complete execution lifecycle:
Parse → Plan → Execute → Validate → Screenshot → Report → Recover

This is the main event loop for autonomous lab completion.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from agents.parser_agent import ParserAgent, ParsedTask, get_parser_agent
from agents.vision_agent import VisionAgent, get_vision_agent
from agents.gui_agent import GUIAgent, get_gui_agent
from agents.qa_agent import QAAgent, get_qa_agent
from agents.report_agent import ReportAgent, TaskResult, ReportMeta, get_report_agent
from agents.recovery_agent import RecoveryAgent, RecoveryTrigger, get_recovery_agent
from core.config import get_config
from core.planner import Planner, ExecutionPlan, get_planner
from core.executor import Executor, PlanExecutionResult, get_executor
from core.state_manager import StateManager, SessionState
from storage.checkpoints import get_checkpoint_manager
from vision.screenshot_engine import Screenshot, get_screenshot_engine


class Orchestrator:
    """
    The brain of the autonomous agent.
    Implements the full execution pipeline and recovery loop.
    """

    def __init__(self, session_id: str = "") -> None:
        cfg = get_config()
        self._cfg = cfg
        self._session_id = session_id or str(uuid.uuid4())
        self._state = StateManager(self._session_id)

        # Agents
        self._parser = get_parser_agent()
        self._vision = get_vision_agent()
        self._gui = get_gui_agent()
        self._qa = get_qa_agent()
        self._report = get_report_agent()
        self._recovery = get_recovery_agent()
        self._planner = get_planner()
        self._executor = get_executor()
        self._screen = get_screenshot_engine()

        # Wire executor callbacks
        self._executor.set_step_callbacks(
            on_complete=self._on_step_complete,
            on_failed=self._on_step_failed,
        )
        self._executor.set_heartbeat(lambda: self._recovery.heartbeat("executing"))

    # ─────────────────────────────────────────────────────────────────────────
    # Main Entry Point
    # ─────────────────────────────────────────────────────────────────────────

    async def run(
        self,
        methodology_path: Path,
        output_path: Path | None = None,
        resume: bool = True,
        report_meta: ReportMeta | None = None,
    ) -> Path:
        """
        Full autonomous execution pipeline.
        Returns path to the generated DOCX report.
        """
        logger.info(f"{'='*60}")
        logger.info(f"Autonomous Lab Agent starting")
        logger.info(f"Methodology: {methodology_path}")
        logger.info(f"Session: {self._session_id}")
        logger.info(f"{'='*60}")

        # Initialize database
        await self._state.initialize(str(methodology_path))

        # Start background recovery services
        watchdog = self._recovery.setup_watchdog(
            hang_callback=self._on_hang,
        )
        self._recovery.start_background_services()

        try:
            report_path = await self._run_pipeline(
                methodology_path, output_path, resume, report_meta
            )
            await self._state.mark_session_complete()
            return report_path

        except Exception as exc:
            logger.exception(f"Orchestrator failed: {exc}")
            await self._state.mark_session_failed(str(exc))
            self._screen.capture_failure("orchestrator_crash")
            raise
        finally:
            self._recovery.stop_background_services()

    async def _run_pipeline(
        self,
        methodology_path: Path,
        output_path: Path | None,
        resume: bool,
        report_meta: ReportMeta | None,
    ) -> Path:
        """Core execution pipeline."""

        # ── Phase 1: Parse ────────────────────────────────────────────────────
        await self._state.transition(SessionState.PARSING)
        self._recovery.heartbeat("parsing")
        methodology = self._parser.parse(methodology_path)
        self._state.set_methodology(methodology)
        logger.info(
            f"Parsed: {len(methodology.tasks)} tasks, "
            f"{len(methodology.get_word_tasks())} word, "
            f"{len(methodology.get_excel_tasks())} excel"
        )

        # ── Phase 2: Resume or start fresh ───────────────────────────────────
        start_from_task = 0
        if resume:
            cp = get_checkpoint_manager().get_resume_point(self._session_id)
            if cp:
                logger.info(f"Resuming from checkpoint: task={cp.task_id} step={cp.step_index}")
                self._state.restore_from_checkpoint(cp)
                start_from_task = next(
                    (i for i, t in enumerate(methodology.tasks)
                     if t.task_id == cp.task_id),
                    0
                )

        # ── Phase 3: Plan all tasks ───────────────────────────────────────────
        await self._state.transition(SessionState.PLANNING)
        await self._state.register_tasks(methodology.tasks)
        plans: dict[str, ExecutionPlan] = {}
        for task in methodology.tasks:
            if not self._state.is_task_completed(task.task_id):
                plan = self._planner.plan_task(task)
                plans[task.task_id] = plan

        logger.info(f"Plans built: {len(plans)}")

        # ── Phase 4: Execute ──────────────────────────────────────────────────
        await self._state.transition(SessionState.EXECUTING)
        for i, task in enumerate(methodology.tasks[start_from_task:], start=start_from_task):
            if self._state.is_task_completed(task.task_id):
                logger.info(f"Skipping completed task: {task.task_id}")
                continue

            await self._execute_task(task, plans.get(task.task_id))

        # ── Phase 5: Build Report ─────────────────────────────────────────────
        await self._state.transition(SessionState.REPORTING)
        self._recovery.heartbeat("reporting")

        report_path = self._report.build_report(
            methodology=methodology,
            task_results=self._state.task_results,
            meta=report_meta,
            output_path=output_path,
        )
        logger.info(f"Report built: {report_path}")
        return report_path

    # ─────────────────────────────────────────────────────────────────────────
    # Task Execution
    # ─────────────────────────────────────────────────────────────────────────

    async def _execute_task(
        self, task: ParsedTask, plan: ExecutionPlan | None
    ) -> None:
        """Execute a single task with full recovery loop."""
        await self._state.start_task(task)
        self._recovery.heartbeat(f"task:{task.task_id}")

        if plan is None:
            plan = self._planner.plan_task(task)

        # Verify application is available before starting
        app_ready = await self._ensure_application(task.application)
        if not app_ready:
            logger.error(f"Application not ready for task {task.task_id}")
            await self._state.fail_task(task, "Application not available")
            return

        # Execute plan with recovery loop
        resume_step = 0
        max_task_attempts = self._cfg.recovery.max_crash_restarts
        task_screenshots: list[Screenshot] = []

        for attempt in range(max_task_attempts):
            if attempt > 0:
                logger.info(f"Task attempt {attempt+1}/{max_task_attempts}: {task.task_id}")
                await asyncio.sleep(2.0)

            plan_result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self._executor.execute_plan(
                    plan,
                    start_from=resume_step,
                    context={
                        "application": task.application,
                        "task_id": task.task_id,
                        "session_id": self._session_id,
                    },
                )
            )

            task_screenshots.extend(plan_result.screenshots)

            if plan_result.completed:
                break

            # Recovery
            await self._state.transition(SessionState.RECOVERING)
            recovery = self._recovery.recover(
                RecoveryTrigger.ACTION_FAILED,
                context={
                    "application": task.application,
                    "session_id": self._session_id,
                    "task_id": task.task_id,
                    "step_index": plan_result.steps_executed,
                    "error": plan_result.error,
                },
            )
            if recovery.succeeded:
                if recovery.retry_from_step >= 0:
                    resume_step = recovery.retry_from_step
                    # Try replanning if we failed multiple times
                    if attempt >= 1:
                        plan = self._planner.replan_from_failure(
                            task, plan, resume_step, plan_result.error
                        )
                await self._state.transition(SessionState.EXECUTING)
            else:
                logger.error(f"Recovery failed for task {task.task_id}")
                break

        # Take final screenshot
        final_shot = self._screen.capture_full(f"final_{task.task_id}")
        task_screenshots.append(final_shot)
        self._state.register_screenshot(
            final_shot,
            for_report=True,
            caption=f"Результат выполнения: {task.title}",
        )

        # Run QA validation
        qa_report = self._qa.validate_task(task)

        # Create task result
        completed = plan_result.completed if 'plan_result' in dir() else False
        task_result = self._report.add_task_result(
            task=task,
            screenshots=task_screenshots,
            completed=completed,
            error=plan_result.error if not completed else "",
        )

        if completed:
            await self._state.complete_task(task, task_result)
        else:
            await self._state.fail_task(task, plan_result.error)
            # Still add to results for partial report
            self._state._task_results.append(task_result)

        # Log summary
        await self._state.log_action(
            component="orchestrator",
            action="execute_task",
            target=task.task_id,
            result="completed" if completed else "failed",
            duration_ms=plan_result.duration_seconds * 1000,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Application Management
    # ─────────────────────────────────────────────────────────────────────────

    async def _ensure_application(self, app: str) -> bool:
        """Ensure the target application is open and focused."""
        if app not in ("word", "excel"):
            return True

        vr = self._vision.verify_application_open(app)
        if vr.passed:
            return True

        # Launch it
        logger.info(f"Launching {app}")
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._executor.execute_single(
                f"open_{app}", target="", value=""
            )
        )
        if result.ok:
            await asyncio.sleep(2.0)
            vr2 = self._vision.verify_application_open(app)
            return vr2.passed

        # Recovery attempt
        recovery = self._recovery.recover(
            RecoveryTrigger.APP_CRASHED,
            context={"application": app},
        )
        return recovery.succeeded

    # ─────────────────────────────────────────────────────────────────────────
    # Callbacks
    # ─────────────────────────────────────────────────────────────────────────

    def _on_step_complete(self, result) -> None:
        logger.debug(
            f"Step ok: {result.step.step_id} "
            f"({result.action_result.duration_ms:.0f}ms)"
        )

    def _on_step_failed(self, result) -> None:
        logger.warning(
            f"Step failed: {result.step.step_id} | {result.action_result.error}"
        )
        # Check for blocking popups
        self._recovery.recover(
            RecoveryTrigger.POPUP_BLOCKING,
            context={"step_id": result.step.step_id},
        )

    def _on_hang(self, event) -> None:
        logger.error(f"Hang detected: {event.details}")
        # Attempt recovery
        state = self._vision.capture_state(include_ui_tree=False, include_ocr=False)
        app = state.detected_application
        if app in ("word", "excel"):
            self._recovery.recover(
                RecoveryTrigger.HANG_DETECTED,
                context={"application": app},
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Status
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._session_id

    def get_status(self) -> dict:
        return {
            "session_id": self._session_id,
            "state": self._state.state.value,
            "elapsed_seconds": self._state.elapsed_seconds(),
            "completed_tasks": len([
                r for r in self._state.task_results if r.completed
            ]),
            "total_tasks": len(self._state.methodology.tasks)
            if self._state.methodology
            else 0,
            "screenshots_taken": len(self._state.report_screenshots),
        }
