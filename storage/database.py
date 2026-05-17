"""
SQLite persistence layer via SQLAlchemy async.
Stores sessions, tasks, steps, screenshots, and execution logs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, AsyncGenerator

import aiosqlite
from loguru import logger
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from core.config import get_config


class Base(DeclarativeBase):
    pass


# ─────────────────────────────────────────────────────────────────────────────
# ORM Models
# ─────────────────────────────────────────────────────────────────────────────

class SessionRecord(Base):
    __tablename__ = "sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    methodology_path = Column(String(512), nullable=False)
    status = Column(String(32), default="pending")  # pending/running/completed/failed
    metadata_ = Column("metadata", JSON, default=dict)

    tasks = relationship("TaskRecord", back_populates="session", cascade="all, delete-orphan")
    logs = relationship("ExecutionLog", back_populates="session", cascade="all, delete-orphan")


class TaskRecord(Base):
    __tablename__ = "tasks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("sessions.id"), nullable=False)
    task_id = Column(String(128), nullable=False)  # logical ID like "word_2_1"
    application = Column(String(32), nullable=False)  # word/excel
    title = Column(String(256), default="")
    description = Column(Text, default="")
    status = Column(String(32), default="pending")
    order_index = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    retry_count = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    expected_result = Column(Text, default="")
    validation_rules = Column(JSON, default=list)

    session = relationship("SessionRecord", back_populates="tasks")
    steps = relationship("StepRecord", back_populates="task", cascade="all, delete-orphan")
    screenshots = relationship("ScreenshotRecord", back_populates="task", cascade="all, delete-orphan")


class StepRecord(Base):
    __tablename__ = "steps"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(36), ForeignKey("tasks.id"), nullable=False)
    step_index = Column(Integer, nullable=False)
    action_type = Column(String(64), nullable=False)  # click/type/shortcut/etc.
    target = Column(String(256), default="")
    parameters = Column(JSON, default=dict)
    status = Column(String(32), default="pending")  # pending/running/success/failed/skipped
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    duration_ms = Column(Float, nullable=True)
    retry_count = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    result_data = Column(JSON, default=dict)

    task = relationship("TaskRecord", back_populates="steps")


class ScreenshotRecord(Base):
    __tablename__ = "screenshots"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id = Column(String(36), ForeignKey("tasks.id"), nullable=True)
    session_id = Column(String(36), nullable=False)
    file_path = Column(String(512), nullable=False)
    caption = Column(Text, default="")
    figure_number = Column(Integer, nullable=True)
    taken_at = Column(DateTime, default=datetime.utcnow)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    is_failure = Column(Boolean, default=False)
    is_report_figure = Column(Boolean, default=False)
    metadata_ = Column("metadata", JSON, default=dict)

    task = relationship("TaskRecord", back_populates="screenshots")


class ExecutionLog(Base):
    __tablename__ = "execution_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(36), ForeignKey("sessions.id"), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    level = Column(String(16), default="INFO")
    component = Column(String(64), default="")
    action = Column(String(128), default="")
    target = Column(String(256), default="")
    result = Column(String(64), default="")
    duration_ms = Column(Float, nullable=True)
    retry_count = Column(Integer, default=0)
    message = Column(Text, default="")
    extra = Column(JSON, default=dict)

    session = relationship("SessionRecord", back_populates="logs")


class CheckpointRecord(Base):
    __tablename__ = "checkpoints"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), nullable=False)
    task_id = Column(String(128), nullable=False)
    step_index = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    state_snapshot = Column(JSON, nullable=False)
    is_valid = Column(Boolean, default=True)


# ─────────────────────────────────────────────────────────────────────────────
# Database Manager
# ─────────────────────────────────────────────────────────────────────────────

class DatabaseManager:
    """Async SQLAlchemy manager. All public methods are coroutines."""

    def __init__(self) -> None:
        cfg = get_config()
        db_url = f"sqlite+aiosqlite:///{cfg.storage.db_path}"
        self._engine = create_async_engine(db_url, echo=False, future=True)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    async def initialize(self) -> None:
        """Create all tables if they don't exist."""
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialized")

    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def create_session(self, methodology_path: str, session_id: str = "") -> str:
        sid = session_id or str(uuid.uuid4())
        async with self._session_factory() as db:
            record = SessionRecord(
                id=sid,
                methodology_path=methodology_path,
                status="pending",
            )
            db.add(record)
            await db.commit()
        logger.info(f"Session created: {sid}")
        return sid

    async def update_session_status(self, session_id: str, status: str) -> None:
        async with self._session_factory() as db:
            await db.execute(
                update(SessionRecord)
                .where(SessionRecord.id == session_id)
                .values(status=status, updated_at=datetime.utcnow())
            )
            await db.commit()

    async def get_session(self, session_id: str) -> SessionRecord | None:
        async with self._session_factory() as db:
            result = await db.execute(
                select(SessionRecord).where(SessionRecord.id == session_id)
            )
            return result.scalar_one_or_none()

    # ── Tasks ─────────────────────────────────────────────────────────────────

    async def create_task(
        self,
        session_id: str,
        task_id: str,
        application: str,
        title: str,
        description: str,
        order_index: int,
        expected_result: str = "",
        validation_rules: list[dict] | None = None,
    ) -> str:
        record_id = str(uuid.uuid4())
        async with self._session_factory() as db:
            record = TaskRecord(
                id=record_id,
                session_id=session_id,
                task_id=task_id,
                application=application,
                title=title,
                description=description,
                order_index=order_index,
                expected_result=expected_result,
                validation_rules=validation_rules or [],
            )
            db.add(record)
            await db.commit()
        return record_id

    async def update_task_status(
        self,
        record_id: str,
        status: str,
        error_message: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if status == "running":
            values["started_at"] = datetime.utcnow()
        elif status in ("completed", "failed"):
            values["completed_at"] = datetime.utcnow()
        if error_message is not None:
            values["error_message"] = error_message
        async with self._session_factory() as db:
            await db.execute(
                update(TaskRecord).where(TaskRecord.id == record_id).values(**values)
            )
            await db.commit()

    async def get_pending_tasks(self, session_id: str) -> list[TaskRecord]:
        async with self._session_factory() as db:
            result = await db.execute(
                select(TaskRecord)
                .where(
                    TaskRecord.session_id == session_id,
                    TaskRecord.status.in_(["pending", "running"]),
                )
                .order_by(TaskRecord.order_index)
            )
            return list(result.scalars().all())

    # ── Steps ─────────────────────────────────────────────────────────────────

    async def upsert_step(
        self,
        task_record_id: str,
        step_index: int,
        action_type: str,
        target: str,
        parameters: dict,
    ) -> str:
        record_id = str(uuid.uuid4())
        async with self._session_factory() as db:
            record = StepRecord(
                id=record_id,
                task_id=task_record_id,
                step_index=step_index,
                action_type=action_type,
                target=target,
                parameters=parameters,
            )
            db.add(record)
            await db.commit()
        return record_id

    async def update_step(
        self,
        step_id: str,
        status: str,
        duration_ms: float | None = None,
        error_message: str | None = None,
        result_data: dict | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if status == "running":
            values["started_at"] = datetime.utcnow()
        elif status in ("success", "failed", "skipped"):
            values["completed_at"] = datetime.utcnow()
        if duration_ms is not None:
            values["duration_ms"] = duration_ms
        if error_message is not None:
            values["error_message"] = error_message
        if result_data is not None:
            values["result_data"] = result_data
        async with self._session_factory() as db:
            await db.execute(
                update(StepRecord).where(StepRecord.id == step_id).values(**values)
            )
            await db.commit()

    # ── Screenshots ───────────────────────────────────────────────────────────

    async def register_screenshot(
        self,
        session_id: str,
        file_path: str,
        caption: str = "",
        task_id: str | None = None,
        figure_number: int | None = None,
        is_failure: bool = False,
        is_report_figure: bool = False,
        width: int | None = None,
        height: int | None = None,
    ) -> str:
        record_id = str(uuid.uuid4())
        async with self._session_factory() as db:
            record = ScreenshotRecord(
                id=record_id,
                session_id=session_id,
                task_id=task_id,
                file_path=file_path,
                caption=caption,
                figure_number=figure_number,
                is_failure=is_failure,
                is_report_figure=is_report_figure,
                width=width,
                height=height,
            )
            db.add(record)
            await db.commit()
        return record_id

    async def get_report_screenshots(self, session_id: str) -> list[ScreenshotRecord]:
        async with self._session_factory() as db:
            result = await db.execute(
                select(ScreenshotRecord)
                .where(
                    ScreenshotRecord.session_id == session_id,
                    ScreenshotRecord.is_report_figure == True,
                )
                .order_by(ScreenshotRecord.figure_number)
            )
            return list(result.scalars().all())

    # ── Checkpoints ───────────────────────────────────────────────────────────

    async def save_checkpoint(
        self,
        session_id: str,
        task_id: str,
        step_index: int,
        state_snapshot: dict,
    ) -> str:
        record_id = str(uuid.uuid4())
        async with self._session_factory() as db:
            # Invalidate older checkpoints for this task
            await db.execute(
                update(CheckpointRecord)
                .where(
                    CheckpointRecord.session_id == session_id,
                    CheckpointRecord.task_id == task_id,
                )
                .values(is_valid=False)
            )
            record = CheckpointRecord(
                id=record_id,
                session_id=session_id,
                task_id=task_id,
                step_index=step_index,
                state_snapshot=state_snapshot,
            )
            db.add(record)
            await db.commit()
        return record_id

    async def get_latest_checkpoint(
        self, session_id: str, task_id: str
    ) -> CheckpointRecord | None:
        async with self._session_factory() as db:
            result = await db.execute(
                select(CheckpointRecord)
                .where(
                    CheckpointRecord.session_id == session_id,
                    CheckpointRecord.task_id == task_id,
                    CheckpointRecord.is_valid == True,
                )
                .order_by(CheckpointRecord.created_at.desc())
                .limit(1)
            )
            return result.scalar_one_or_none()

    # ── Logging ───────────────────────────────────────────────────────────────

    async def log_action(
        self,
        session_id: str,
        component: str,
        action: str,
        target: str = "",
        result: str = "",
        duration_ms: float | None = None,
        retry_count: int = 0,
        level: str = "INFO",
        message: str = "",
        extra: dict | None = None,
    ) -> None:
        async with self._session_factory() as db:
            record = ExecutionLog(
                session_id=session_id,
                level=level,
                component=component,
                action=action,
                target=target,
                result=result,
                duration_ms=duration_ms,
                retry_count=retry_count,
                message=message,
                extra=extra or {},
            )
            db.add(record)
            await db.commit()

    async def close(self) -> None:
        await self._engine.dispose()
        logger.info("Database connection closed")


# Module-level singleton
_db: DatabaseManager | None = None


async def get_db() -> DatabaseManager:
    global _db
    if _db is None:
        _db = DatabaseManager()
        await _db.initialize()
    return _db
