"""
State Machine for execution lifecycle management.
Tracks session, task, and step states with SQLite persistence.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from enum import Enum
from typing import Any

from loguru import logger

from agents.parser_agent import ParsedTask, ParsedMethodology
from agents.report_agent import TaskResult
from storage.checkpoints import AgentCheckpoint, get_checkpoint_manager
from storage.database import DatabaseManager, get_db
from vision.screenshot_engine import Screenshot


class SessionState(str, Enum):
    IDLE = "idle"
    PARSING = "parsing"
    PLANNING = "planning"
    EXECUTING = "executing"
    RECOVERING = "recovering"
    REPORTING = "reporting"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class StateManager:
    """
    Manages execution state across the full session lifecycle.
    All state mutations are persisted immediately to SQLite + checkpoint files.
    """

    def __init__(self, session_id: str = "") -> None:
        self._session_id = session_id or str(uuid.uuid4())
        self._state = SessionState.IDLE
        self._checkpoint_mgr = get_checkpoint_manager()
        self._db: DatabaseManager | None = None

        # In-memory working state
        self._methodology: ParsedMethodology | None = None
        self._task_record_ids: dict[str, str] = {}  # task_id -> db record id
        self._task_states: dict[str, TaskState] = {}
        self._current_task: ParsedTask | None = None
        self._current_step_index: int = 0

        # Accumulated results
        self._task_results: list[TaskResult] = []
        self._session_screenshots: list[Screenshot] = []
        self._report_screenshots: list[Screenshot] = []

        # Execution metadata
        self._started_at: float = 0.0
        self._checkpoint = AgentCheckpoint(session_id=self._session_id)

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def methodology(self) -> ParsedMethodology | None:
        return self._methodology

    @property
    def current_task(self) -> ParsedTask | None:
        return self._current_task

    @property
    def current_step_index(self) -> int:
        return self._current_step_index

    @property
    def task_results(self) -> list[TaskResult]:
        return self._task_results

    @property
    def report_screenshots(self) -> list[Screenshot]:
        return self._report_screenshots

    async def initialize(self, methodology_path: str) -> None:
        """Set up database session and load any existing checkpoint."""
        self._db = await get_db()
        await self._db.create_session(methodology_path, self._session_id)
        await self._db.update_session_status(self._session_id, "running")
        self._started_at = time.time()
        logger.info(f"StateManager initialized: session={self._session_id}")

    async def transition(self, new_state: SessionState) -> None:
        old = self._state
        self._state = new_state
        logger.info(f"State: {old.value} → {new_state.value}")
        if self._db:
            await self._db.update_session_status(self._session_id, new_state.value)

    def set_methodology(self, methodology: ParsedMethodology) -> None:
        self._methodology = methodology

    async def register_tasks(self, tasks: list[ParsedTask]) -> None:
        """Register all tasks in the database."""
        if not self._db:
            return
        for task in tasks:
            record_id = await self._db.create_task(
                session_id=self._session_id,
                task_id=task.task_id,
                application=task.application,
                title=task.title,
                description=task.description,
                order_index=task.order_index,
                expected_result=task.expected_result,
                validation_rules=[r.model_dump() for r in task.validation_rules],
            )
            self._task_record_ids[task.task_id] = record_id
            self._task_states[task.task_id] = TaskState.PENDING

    async def start_task(self, task: ParsedTask) -> None:
        self._current_task = task
        self._current_step_index = 0
        self._task_states[task.task_id] = TaskState.RUNNING
        record_id = self._task_record_ids.get(task.task_id)
        if record_id and self._db:
            await self._db.update_task_status(record_id, "running")
        logger.info(f"Task started: {task.task_id} ({task.title})")

    async def complete_task(self, task: ParsedTask, result: TaskResult) -> None:
        self._task_states[task.task_id] = TaskState.COMPLETED
        self._task_results.append(result)
        self._checkpoint.completed_task_ids.append(task.task_id)
        record_id = self._task_record_ids.get(task.task_id)
        if record_id and self._db:
            await self._db.update_task_status(record_id, "completed")
        logger.info(f"Task completed: {task.task_id}")
        await self._save_checkpoint()

    async def fail_task(self, task: ParsedTask, error: str = "") -> None:
        self._task_states[task.task_id] = TaskState.FAILED
        record_id = self._task_record_ids.get(task.task_id)
        if record_id and self._db:
            await self._db.update_task_status(record_id, "failed", error)
        logger.error(f"Task failed: {task.task_id} | {error}")
        await self._save_checkpoint()

    def advance_step(self) -> None:
        self._current_step_index += 1
        self._checkpoint.step_index = self._current_step_index

    def set_step(self, idx: int) -> None:
        self._current_step_index = idx
        self._checkpoint.step_index = idx

    def register_screenshot(
        self, shot: Screenshot, for_report: bool = True, caption: str = ""
    ) -> None:
        self._session_screenshots.append(shot)
        if for_report:
            shot.caption = caption
            self._report_screenshots.append(shot)
            num = len(self._report_screenshots)
            shot.figure_number = num
            self._checkpoint.screenshot_registry[num] = str(shot.path)
            self._checkpoint.next_figure_number = num + 1

    def get_task_screenshots(self, task_id: str) -> list[Screenshot]:
        """Get screenshots associated with a specific task."""
        return [s for s in self._report_screenshots if hasattr(s, "task_id") and s.task_id == task_id]

    def get_remaining_tasks(self) -> list[ParsedTask]:
        if not self._methodology:
            return []
        return [
            t for t in self._methodology.tasks
            if self._task_states.get(t.task_id, TaskState.PENDING) == TaskState.PENDING
        ]

    def is_task_completed(self, task_id: str) -> bool:
        return self._task_states.get(task_id) == TaskState.COMPLETED

    async def _save_checkpoint(self) -> None:
        if self._current_task:
            self._checkpoint.task_id = self._current_task.task_id
        self._checkpoint.step_index = self._current_step_index
        self._checkpoint_mgr.save(self._checkpoint)

    def restore_from_checkpoint(self, cp: AgentCheckpoint) -> None:
        """Restore execution state from a checkpoint."""
        self._checkpoint = cp
        for task_id in cp.completed_task_ids:
            self._task_states[task_id] = TaskState.COMPLETED
        self._current_step_index = cp.step_index
        self._checkpoint.next_figure_number = cp.next_figure_number
        logger.info(
            f"Restored from checkpoint: {len(cp.completed_task_ids)} completed tasks, "
            f"step={cp.step_index}"
        )

    def elapsed_seconds(self) -> float:
        return time.time() - self._started_at if self._started_at else 0.0

    async def mark_session_complete(self) -> None:
        await self.transition(SessionState.COMPLETED)
        self._checkpoint_mgr.delete_session(self._session_id)
        logger.info(f"Session completed in {self.elapsed_seconds():.1f}s")

    async def mark_session_failed(self, error: str = "") -> None:
        await self.transition(SessionState.FAILED)
        if self._db:
            await self._db.update_session_status(self._session_id, "failed")
        logger.error(f"Session failed: {error}")

    async def log_action(
        self,
        component: str,
        action: str,
        target: str = "",
        result: str = "",
        duration_ms: float | None = None,
        level: str = "INFO",
        message: str = "",
    ) -> None:
        if self._db:
            await self._db.log_action(
                session_id=self._session_id,
                component=component,
                action=action,
                target=target,
                result=result,
                duration_ms=duration_ms,
                level=level,
                message=message,
            )
