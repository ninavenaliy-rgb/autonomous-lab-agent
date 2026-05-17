"""
Checkpoint serialization and recovery management.
Provides fast serialization of agent state for crash recovery.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from core.config import get_config


@dataclass
class AgentCheckpoint:
    """Full recoverable state snapshot."""

    checkpoint_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    task_id: str = ""
    step_index: int = 0
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Completed work
    completed_task_ids: list[str] = field(default_factory=list)
    completed_step_indices: list[int] = field(default_factory=list)

    # Screenshot registry: {figure_number: file_path}
    screenshot_registry: dict[int, str] = field(default_factory=dict)
    next_figure_number: int = 1

    # Active application state
    active_application: str = ""  # word/excel/none
    active_document_path: str = ""

    # Report accumulation state
    report_sections: list[dict] = field(default_factory=list)

    # Execution counters
    total_retries: int = 0
    total_failures: int = 0

    # Custom agent data
    agent_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentCheckpoint":
        return cls(**data)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "AgentCheckpoint":
        return cls.from_dict(json.loads(text))


class CheckpointManager:
    """Writes and reads checkpoints to disk + database."""

    def __init__(self) -> None:
        cfg = get_config()
        self._dir = cfg.storage.checkpoint_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str, task_id: str) -> Path:
        safe_task = task_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"{session_id}_{safe_task}.json"

    def save(self, checkpoint: AgentCheckpoint) -> Path:
        path = self._path(checkpoint.session_id, checkpoint.task_id)
        path.write_text(checkpoint.to_json(), encoding="utf-8")
        logger.debug(
            f"Checkpoint saved | session={checkpoint.session_id} "
            f"task={checkpoint.task_id} step={checkpoint.step_index}"
        )
        return path

    def load(self, session_id: str, task_id: str) -> AgentCheckpoint | None:
        path = self._path(session_id, task_id)
        if not path.exists():
            return None
        try:
            cp = AgentCheckpoint.from_json(path.read_text(encoding="utf-8"))
            logger.info(
                f"Checkpoint restored | session={session_id} "
                f"task={task_id} step={cp.step_index}"
            )
            return cp
        except (json.JSONDecodeError, TypeError) as exc:
            logger.error(f"Failed to load checkpoint {path}: {exc}")
            return None

    def load_session(self, session_id: str) -> list[AgentCheckpoint]:
        """Load all checkpoints for a session, sorted by task order."""
        checkpoints = []
        for path in sorted(self._dir.glob(f"{session_id}_*.json")):
            try:
                cp = AgentCheckpoint.from_json(path.read_text(encoding="utf-8"))
                checkpoints.append(cp)
            except Exception as exc:
                logger.warning(f"Skipping corrupt checkpoint {path}: {exc}")
        return checkpoints

    def delete(self, session_id: str, task_id: str) -> None:
        path = self._path(session_id, task_id)
        if path.exists():
            path.unlink()
            logger.debug(f"Checkpoint deleted: {path.name}")

    def delete_session(self, session_id: str) -> int:
        deleted = 0
        for path in self._dir.glob(f"{session_id}_*.json"):
            path.unlink()
            deleted += 1
        logger.info(f"Deleted {deleted} checkpoints for session {session_id}")
        return deleted

    def get_resume_point(self, session_id: str) -> AgentCheckpoint | None:
        """Find the most recent valid checkpoint to resume from."""
        checkpoints = self.load_session(session_id)
        if not checkpoints:
            return None
        # Sort by step_index descending, pick latest
        checkpoints.sort(key=lambda c: (c.task_id, c.step_index), reverse=True)
        latest = checkpoints[0]
        logger.info(
            f"Resume point found: task={latest.task_id} step={latest.step_index}"
        )
        return latest


# Module-level singleton
_checkpoint_manager: CheckpointManager | None = None


def get_checkpoint_manager() -> CheckpointManager:
    global _checkpoint_manager
    if _checkpoint_manager is None:
        _checkpoint_manager = CheckpointManager()
    return _checkpoint_manager
