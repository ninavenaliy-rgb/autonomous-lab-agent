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
            # Initial progress message
            ref_hint = " + образец" if session.reference_path else ""
            msg = await self._send(
                f"⚙️ *Агент запущен*{ref_hint}\n\n"
                "🔍 Читаю методичку...",
            )
            self._progress_msg_id = msg.message_id

            # Parse reference document if provided
            reference_text = ""
            if session.reference_path and session.reference_path.exists():
                from agents.reference_parser import parse_reference
                ref_doc = parse_reference(session.reference_path)
                if ref_doc:
                    reference_text = ref_doc.llm_context
                    logger.info(f"Reference loaded: {len(reference_text)} chars context")

            # Build report metadata
            meta = ReportMeta(
                title=Path(session.original_filename).stem,
                student=session.student_name,
                group=session.group,
                teacher=session.teacher,
                lab_number=session.lab_number,
                university=get_config().report.university,
                department=get_config().report.department,
            )

            # Build orchestrator with progress hooks
            orchestrator = Orchestrator(session_id=session.session_id)

            # Monkey-patch state transitions for Telegram updates
            original_transition = orchestrator._state.transition

            async def patched_transition(new_state):
                await original_transition(new_state)
                await self._on_state_change(new_state.value)

            orchestrator._state.transition = patched_transition

            # Run the agent
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

            # Send completion message
            await self._edit_progress(
                f"✅ *Готово за {elapsed:.0f} сек!*\n\n"
                f"Задания выполнены. Отправляю отчёт..."
            )

            # Send the report file
            await self._send_report(report_path)

            # Send execution summary
            status = orchestrator.get_status()
            await self._send(
                f"📊 *Итоги:*\n"
                f"✅ Заданий выполнено: {status['completed_tasks']}/{status['total_tasks']}\n"
                f"📸 Скриншотов: {status['screenshots_taken']}\n"
                f"⏱ Время: {elapsed:.0f} сек\n\n"
                f"Отправь новый файл для следующей лабораторной."
            )

        except Exception as exc:
            session.state = "failed"
            session.error = str(exc)
            logger.exception(f"Agent runner failed: {exc}")

            # Send failure screenshot if exists
            cfg = get_config()
            failures = sorted(cfg.storage.screenshot_dir.glob("FAILURE_*.png"))
            if failures:
                latest = failures[-1]
                await self._send_photo(
                    latest,
                    caption=f"❌ Агент завершился с ошибкой:\n`{str(exc)[:300]}`",
                )
            else:
                await self._send(
                    f"❌ *Ошибка агента:*\n`{str(exc)[:500]}`\n\n"
                    "Попробуй заново или проверь, что Word/Excel установлены и запускаются.",
                )

    async def _on_state_change(self, state: str) -> None:
        """Update Telegram progress message on state transitions."""
        state_messages = {
            "parsing": "🔍 Читаю методичку...",
            "planning": "🗺 Составляю план выполнения...",
            "executing": "⚙️ Выполняю задания...",
            "recovering": "🔧 Устраняю проблему...",
            "reporting": "📝 Составляю отчёт...",
        }
        text = state_messages.get(state)
        if text:
            await self._edit_progress(f"⚙️ *Агент работает*\n\n{text}")

    async def _send(self, text: str) -> any:
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
            pass  # Message might be too old to edit

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
            await self._send(f"⚠️ Не удалось отправить файл: {e}\nОтчёт сохранён на сервере: `{path}`")

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
