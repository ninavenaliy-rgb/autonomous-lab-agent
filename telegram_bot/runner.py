"""
AgentRunner — runs the autonomous agent and streams progress to Telegram.
Runs as an asyncio task so the bot stays responsive during execution.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from loguru import logger
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError

from agents.report_agent import ReportMeta
from core.config import get_config
from core.orchestrator import Orchestrator
from telegram_bot.session import BotSession


class AgentRunner:
    """
    Wraps the Orchestrator and sends real-time Telegram updates.
    Supports two modes:
      - clone_mode: copy reference doc, substitute student info (seconds)
      - agent_mode: full autonomous execution (minutes)
    """

    def __init__(self, bot: Bot, chat_id: int, session: BotSession) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._session = session
        self._progress_msg_id: int | None = None

    async def run(self) -> None:
        session = self._session
        session.state = "running"
        t0 = time.time()
        try:
            if session.clone_mode and session.reference_path:
                await self._run_clone(session, t0)
            else:
                await self._run_agent(session, t0)
        except Exception as exc:
            session.state = "failed"
            session.error = str(exc)
            logger.exception(f"Runner failed: {exc}")
            cfg = get_config()
            failures = sorted(cfg.storage.screenshot_dir.glob("FAILURE_*.png"))
            if failures:
                await self._send_photo(
                    failures[-1],
                    caption=f"❌ Ошибка:\n`{str(exc)[:300]}`",
                )
            else:
                await self._send(f"❌ *Ошибка:*\n`{str(exc)[:500]}`")

    # ─────────────────────────────────────────────────────────────────────────
    # Clone mode
    # ─────────────────────────────────────────────────────────────────────────

    async def _run_clone(self, session: BotSession, t0: float) -> None:
        """Exact copy of reference doc with student name/group substituted."""
        msg = await self._send("📄 *Копирую отчёт...*")
        self._progress_msg_id = msg.message_id

        from agents.doc_cloner import clone_document
        cfg = get_config()
        output_path = (
            cfg.storage.report_output_dir
            / f"report_{session.session_id[:8]}.docx"
        )

        report_path = clone_document(
            reference_path=session.reference_path,
            output_path=output_path,
            new_name=session.student_name,
            new_group=session.group,
        )

        session.report_path = report_path
        session.state = "done"
        elapsed = time.time() - t0

        await self._edit_progress(f"✅ *Готово за {elapsed:.0f} сек!* Отправляю...")
        await self._send_report(report_path)
        await self._send(
            "📄 Готово — точная копия с твоими данными.\n"
            "Отправь новый файл для следующей работы."
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Agent mode
    # ─────────────────────────────────────────────────────────────────────────

    async def _run_agent(self, session: BotSession, t0: float) -> None:
        """Full autonomous execution via Orchestrator."""
        ref_hint = " + образец" if session.reference_path else ""
        msg = await self._send(
            f"⚙️ *Агент запущен*{ref_hint}\n\n🔍 Читаю методичку..."
        )
        self._progress_msg_id = msg.message_id

        # Load reference context if available
        reference_text = ""
        if session.reference_path and session.reference_path.exists():
            from agents.reference_parser import parse_reference
            ref_doc = parse_reference(session.reference_path)
            if ref_doc:
                reference_text = ref_doc.llm_context
                logger.info(f"Reference loaded: {len(reference_text)} chars")

        meta = ReportMeta(
            title=Path(session.original_filename).stem,
            student=session.student_name,
            group=session.group,
            teacher=session.teacher,
            lab_number=session.lab_number,
            university=get_config().report.university,
            department=get_config().report.department,
        )

        orchestrator = Orchestrator(session_id=session.session_id)

        original_transition = orchestrator._state.transition

        async def patched_transition(new_state):
            await original_transition(new_state)
            await self._on_state_change(new_state.value)

        orchestrator._state.transition = patched_transition

        cfg = get_config()
        output_path = (
            cfg.storage.report_output_dir
            / f"report_{session.session_id[:8]}.docx"
        )

        report_path = await orchestrator.run(
            methodology_path=session.methodology_path,
            output_path=output_path,
            resume=True,
            report_meta=meta,
            reference_text=reference_text,
        )

        session.report_path = report_path
        session.state = "done"
        elapsed = time.time() - t0

        await self._edit_progress(
            f"✅ *Готово за {elapsed:.0f} сек!*\n\nЗадания выполнены. Отправляю отчёт..."
        )
        await self._send_report(report_path)

        status = orchestrator.get_status()
        await self._send(
            f"📊 *Итоги:*\n"
            f"✅ Заданий: {status['completed_tasks']}/{status['total_tasks']}\n"
            f"📸 Скриншотов: {status['screenshots_taken']}\n"
            f"⏱ Время: {elapsed:.0f} сек\n\n"
            f"Отправь новый файл для следующей лабораторной."
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    async def _on_state_change(self, state: str) -> None:
        messages = {
            "parsing": "🔍 Читаю методичку...",
            "planning": "🗺 Составляю план...",
            "executing": "⚙️ Выполняю задания...",
            "recovering": "🔧 Устраняю проблему...",
            "reporting": "📝 Составляю отчёт...",
        }
        text = messages.get(state)
        if text:
            await self._edit_progress(f"⚙️ *Агент работает*\n\n{text}")

    async def _send(self, text: str):
        try:
            return await self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError as e:
            logger.warning(f"Telegram send failed: {e}")

    async def _edit_progress(self, text: str) -> None:
        if not self._progress_msg_id:
            return
        try:
            await self._bot.edit_message_text(
                chat_id=self._chat_id,
                message_id=self._progress_msg_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
        except TelegramError:
            pass

    async def _send_report(self, path: Path) -> None:
        try:
            with open(path, "rb") as f:
                await self._bot.send_document(
                    chat_id=self._chat_id,
                    document=f,
                    filename=path.name,
                    caption="📄 Готовый отчёт по лабораторной работе",
                )
        except TelegramError as e:
            logger.error(f"Failed to send report: {e}")
            await self._send(f"⚠️ Не удалось отправить файл: {e}")

    async def _send_photo(self, path: Path, caption: str = "") -> None:
        try:
            with open(path, "rb") as f:
                await self._bot.send_photo(
                    chat_id=self._chat_id,
                    photo=f,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                )
        except TelegramError as e:
            logger.warning(f"Failed to send photo: {e}")
