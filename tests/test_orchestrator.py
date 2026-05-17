"""Integration-level tests for planner, executor, and state manager."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.parser_agent import ParsedTask, AtomicStep, ParsedMethodology
from core.planner import Planner, ExecutionPlan
from core.state_manager import StateManager, SessionState, TaskState
from storage.checkpoints import AgentCheckpoint, CheckpointManager


# ─────────────────────────────────────────────────────────────────────────────
# Planner tests
# ─────────────────────────────────────────────────────────────────────────────

class TestPlanner:
    def _make_task(
        self,
        task_id: str = "word_1",
        app: str = "word",
        steps: list[AtomicStep] | None = None,
    ) -> ParsedTask:
        return ParsedTask(
            task_id=task_id,
            application=app,
            title="Test task",
            description="Do something in Word",
            steps=steps or [],
        )

    def test_plan_task_with_open_app(self):
        planner = Planner()
        task = self._make_task()
        plan = planner.plan_task(task, with_open_app=True)
        actions = [s.action for s in plan.steps]
        assert "open_word" in actions

    def test_plan_task_without_open_app(self):
        planner = Planner()
        task = self._make_task()
        plan = planner.plan_task(task, with_open_app=False)
        actions = [s.action for s in plan.steps]
        assert "open_word" not in actions

    def test_plan_excel_task(self):
        planner = Planner()
        task = self._make_task(app="excel")
        plan = planner.plan_task(task, with_open_app=True)
        actions = [s.action for s in plan.steps]
        assert "open_excel" in actions
        assert "open_word" not in actions

    def test_plan_step_ids_sequential(self):
        planner = Planner()
        task = self._make_task(steps=[
            AtomicStep(step_id="x1", action="type", value="hello"),
            AtomicStep(step_id="x2", action="save_file"),
        ])
        plan = planner.plan_task(task, with_open_app=False)
        for i, step in enumerate(plan.steps):
            assert step.step_id == f"s{i+1}"

    def test_replan_from_failure_replaces_step(self):
        planner = Planner()
        steps = [
            AtomicStep(step_id="s1", action="open_word"),
            AtomicStep(step_id="s2", action="click", target="OK"),
            AtomicStep(step_id="s3", action="save_file"),
        ]
        plan = ExecutionPlan(task_id="t1", steps=steps)
        task = self._make_task()
        new_plan = planner.replan_from_failure(task, plan, failed_step_index=1, error="not found")
        # The failed step should be replaced or alternatives inserted
        assert new_plan.total_steps() >= 2  # At least remaining + alternatives

    def test_get_step_valid_index(self):
        steps = [AtomicStep(step_id="s1", action="type")]
        plan = ExecutionPlan(task_id="t1", steps=steps)
        assert plan.get_step(0) is not None
        assert plan.get_step(1) is None
        assert plan.get_step(-1) is None

    def test_remaining_steps(self):
        steps = [
            AtomicStep(step_id=f"s{i}", action="wait_ms")
            for i in range(5)
        ]
        plan = ExecutionPlan(task_id="t1", steps=steps)
        remaining = plan.remaining_steps(3)
        assert len(remaining) == 2

    def test_heuristic_steps_word_table(self):
        planner = Planner()
        task = ParsedTask(
            task_id="t1",
            application="word",
            title="Вставка таблицы",
            description="Создайте таблицу с данными в Word",
            steps=[],
        )
        steps = planner._generate_steps_heuristic(task)
        actions = [s.action for s in steps]
        assert "insert_table" in actions

    def test_infer_shortcut_save(self):
        planner = Planner()
        assert planner._infer_shortcut("Файл/Сохранить") == "ctrl+s"

    def test_infer_shortcut_unknown(self):
        planner = Planner()
        result = planner._infer_shortcut("UnknownMenuPath")
        assert result == "escape"


# ─────────────────────────────────────────────────────────────────────────────
# StateManager tests
# ─────────────────────────────────────────────────────────────────────────────

class TestStateManager:
    def _make_methodology(self) -> ParsedMethodology:
        return ParsedMethodology(
            title="Lab 1",
            document_path="/tmp/lab.docx",
            tasks=[
                ParsedTask(task_id="w1", application="word", title="Task 1", description=""),
                ParsedTask(task_id="w2", application="word", title="Task 2", description=""),
            ],
        )

    @pytest.mark.asyncio
    async def test_initial_state(self):
        sm = StateManager("test-session")
        assert sm.state == SessionState.IDLE
        assert sm.current_task is None

    @pytest.mark.asyncio
    async def test_transition(self):
        sm = StateManager("test-session")
        # Mock DB
        sm._db = MagicMock()
        sm._db.update_session_status = AsyncMock()
        await sm.transition(SessionState.PARSING)
        assert sm.state == SessionState.PARSING

    def test_get_remaining_tasks_all_pending(self):
        sm = StateManager("test-session")
        m = self._make_methodology()
        sm.set_methodology(m)
        sm._task_states = {t.task_id: TaskState.PENDING for t in m.tasks}
        remaining = sm.get_remaining_tasks()
        assert len(remaining) == 2

    def test_get_remaining_tasks_one_completed(self):
        sm = StateManager("test-session")
        m = self._make_methodology()
        sm.set_methodology(m)
        sm._task_states = {
            "w1": TaskState.COMPLETED,
            "w2": TaskState.PENDING,
        }
        remaining = sm.get_remaining_tasks()
        assert len(remaining) == 1
        assert remaining[0].task_id == "w2"

    def test_is_task_completed(self):
        sm = StateManager("test-session")
        sm._task_states["t1"] = TaskState.COMPLETED
        assert sm.is_task_completed("t1")
        assert not sm.is_task_completed("t2")

    def test_advance_step(self):
        sm = StateManager("test-session")
        sm._checkpoint.step_index = 0
        sm.advance_step()
        assert sm.current_step_index == 1
        sm.advance_step()
        assert sm.current_step_index == 2

    def test_restore_from_checkpoint(self):
        sm = StateManager("test-session")
        cp = AgentCheckpoint(
            session_id="test-session",
            task_id="w2",
            step_index=3,
            completed_task_ids=["w1"],
        )
        sm.restore_from_checkpoint(cp)
        assert sm.is_task_completed("w1")
        assert not sm.is_task_completed("w2")
        assert sm.current_step_index == 3


# ─────────────────────────────────────────────────────────────────────────────
# CheckpointManager tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckpointManager:
    def test_save_and_load(self, tmp_path):
        from core.config import get_config
        cfg = get_config()
        original_dir = cfg.storage.checkpoint_dir

        mgr = CheckpointManager.__new__(CheckpointManager)
        mgr._dir = tmp_path

        cp = AgentCheckpoint(
            session_id="sess1",
            task_id="task_1",
            step_index=5,
            completed_task_ids=["t0"],
        )
        mgr.save(cp)
        loaded = mgr.load("sess1", "task_1")
        assert loaded is not None
        assert loaded.step_index == 5
        assert "t0" in loaded.completed_task_ids

    def test_load_nonexistent_returns_none(self, tmp_path):
        mgr = CheckpointManager.__new__(CheckpointManager)
        mgr._dir = tmp_path
        result = mgr.load("no_session", "no_task")
        assert result is None

    def test_delete(self, tmp_path):
        mgr = CheckpointManager.__new__(CheckpointManager)
        mgr._dir = tmp_path
        cp = AgentCheckpoint(session_id="s1", task_id="t1", step_index=0)
        mgr.save(cp)
        assert mgr.load("s1", "t1") is not None
        mgr.delete("s1", "t1")
        assert mgr.load("s1", "t1") is None

    def test_delete_session(self, tmp_path):
        mgr = CheckpointManager.__new__(CheckpointManager)
        mgr._dir = tmp_path
        for i in range(3):
            cp = AgentCheckpoint(session_id="s1", task_id=f"t{i}", step_index=i)
            mgr.save(cp)
        deleted = mgr.delete_session("s1")
        assert deleted == 3

    def test_get_resume_point(self, tmp_path):
        mgr = CheckpointManager.__new__(CheckpointManager)
        mgr._dir = tmp_path
        cp1 = AgentCheckpoint(session_id="s1", task_id="t1", step_index=2)
        cp2 = AgentCheckpoint(session_id="s1", task_id="t2", step_index=0)
        mgr.save(cp1)
        mgr.save(cp2)
        resume = mgr.get_resume_point("s1")
        assert resume is not None
        # Should pick the latest
        assert resume.step_index in (0, 2)

    def test_checkpoint_serialization_roundtrip(self):
        cp = AgentCheckpoint(
            session_id="abc",
            task_id="task_1",
            step_index=7,
            completed_task_ids=["t0", "t1"],
            screenshot_registry={1: "/tmp/shot1.png"},
            next_figure_number=2,
        )
        json_str = cp.to_json()
        restored = AgentCheckpoint.from_json(json_str)
        assert restored.session_id == "abc"
        assert restored.step_index == 7
        assert restored.completed_task_ids == ["t0", "t1"]
        assert restored.next_figure_number == 2
