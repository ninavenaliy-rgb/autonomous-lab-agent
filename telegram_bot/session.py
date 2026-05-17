"""
Bot session state: stores per-user data between conversation steps.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BotSession:
    user_id: int
    methodology_path: Path
    original_filename: str = ""
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    student_name: str = ""
    group: str = ""
    teacher: str = ""
    lab_number: str = ""

    # Runtime state
    state: str = "idle"          # idle / running / done / failed
    current_task: str = ""
    tasks_done: int = 0
    tasks_total: int = 0
    progress_message_id: int = 0
    report_path: Path | None = None
    error: str = ""

    def status_text(self) -> str:
        icons = {
            "idle": "⏳",
            "running": "⚙️",
            "done": "✅",
            "failed": "❌",
        }
        icon = icons.get(self.state, "❓")
        lines = [f"{icon} *Статус:* {self.state}"]
        if self.current_task:
            lines.append(f"📌 Текущее задание: {self.current_task}")
        if self.tasks_total:
            lines.append(f"📊 Прогресс: {self.tasks_done}/{self.tasks_total}")
        if self.error:
            lines.append(f"⚠️ Ошибка: {self.error[:200]}")
        return "\n".join(lines)


class SessionStore:
    """In-memory store mapping user_id → BotSession."""

    def __init__(self) -> None:
        self._sessions: dict[int, BotSession] = {}

    def get(self, user_id: int) -> BotSession | None:
        return self._sessions.get(user_id)

    def set(self, user_id: int, session: BotSession) -> None:
        self._sessions[user_id] = session

    def remove(self, user_id: int) -> None:
        self._sessions.pop(user_id, None)
