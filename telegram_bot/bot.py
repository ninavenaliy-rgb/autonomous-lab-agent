"""
Telegram Bot frontend for the Autonomous Lab Agent.
User sends a .docx/.pdf methodology → bot runs the agent → returns finished report.

Run:
    python telegram_bot/bot.py

Required env vars:
    TELEGRAM_BOT_TOKEN=123456:ABC-...
    ANTHROPIC_API_KEY=sk-ant-...
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from telegram import (
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from core.config import get_config
from telegram_bot.session import BotSession, SessionStore
from telegram_bot.runner import AgentRunner

# Conversation states
WAITING_FILE = 1
WAITING_MODE = 2       # after first file: choose methodology vs clone
WAITING_REFERENCE = 3  # methodology mode: optional reference upload
WAITING_STUDENT = 4
WAITING_GROUP = 5
CONFIRM_RUN = 6

store = SessionStore()


# ─────────────────────────────────────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        "Отправь мне файл методички (.docx или .pdf) и я выполню все задания.\n\n"
        "Команды:\n"
        "/run — начать новое задание\n"
        "/status — статус текущего выполнения\n"
        "/cancel — отменить\n"
        "/help — помощь",
    )
    return WAITING_FILE


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "📖 *Как пользоваться:*\n\n"
        "1\\. Отправь файл методички \\(.docx или .pdf\\)\n"
        "2\\. Укажи имя студента и группу \\(или пропусти\\)\n"
        "3\\. Подтверди запуск\n"
        "4\\. Дождись отчёта 🎉\n\n"
        "*Что умеет агент:*\n"
        "• Работает с Microsoft Word и Excel\n"
        "• Понимает задания на русском языке\n"
        "• Делает скриншоты каждого шага\n"
        "• Генерирует отчёт по ГОСТ\n"
        "• Восстанавливается после сбоев\n\n"
        "Если что-то пошло не так — агент пришлёт скриншот ошибки\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    session = store.get(uid)
    if not session:
        await update.message.reply_text("Нет активных заданий. Отправь файл методички.")
        return
    await update.message.reply_text(session.status_text())


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    store.remove(uid)
    await update.message.reply_text("❌ Отменено. Отправь новый файл, чтобы начать заново.")
    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# File handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    doc: Document = update.message.document
    if not doc:
        await update.message.reply_text("Пожалуйста, отправь файл (.docx или .pdf).")
        return WAITING_FILE

    fname = doc.file_name or ""
    if not (fname.endswith(".docx") or fname.endswith(".pdf")):
        await update.message.reply_text(
            "❌ Поддерживаются только .docx и .pdf файлы.\n"
            "Отправь файл методички."
        )
        return WAITING_FILE

    # Download file
    cfg = get_config()
    downloads = cfg.log_dir / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    local_path = downloads / f"{uuid.uuid4().hex}_{fname}"

    msg = await update.message.reply_text("⏬ Скачиваю файл...")
    tg_file = await ctx.bot.get_file(doc.file_id)
    await tg_file.download_to_drive(str(local_path))

    # Create session
    uid = update.effective_user.id
    session = BotSession(
        user_id=uid,
        methodology_path=local_path,
        original_filename=fname,
    )
    store.set(uid, session)
    ctx.user_data["session_id"] = session.session_id

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📋 Это методичка — выполнить задания",
            callback_data="mode_agent",
        )],
        [InlineKeyboardButton(
            "📄 Это готовый отчёт — скопировать, сменить имя",
            callback_data="mode_clone",
        )],
    ])
    await msg.edit_text(
        f"✅ Файл получен: *{fname}*\n\nЧто мне с ним сделать?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )
    return WAITING_MODE


# ─────────────────────────────────────────────────────────────────────────────
# Mode selection (after first file upload)
# ─────────────────────────────────────────────────────────────────────────────

async def handle_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid = update.effective_user.id
    session = store.get(uid)
    if not session:
        return ConversationHandler.END

    if query.data == "mode_clone":
        # Clone mode: the file IS the reference
        session.reference_path = session.methodology_path
        session.clone_mode = True
        await query.edit_message_text(
            "📄 *Режим копирования*\n\n"
            "Введи своё ФИО (как должно быть в отчёте).\n"
            "Или /skip чтобы оставить как есть:",
            parse_mode=ParseMode.MARKDOWN,
        )
        return WAITING_STUDENT
    else:
        # Agent mode: file is methodology, optionally ask for reference
        session.clone_mode = False
        await query.edit_message_text(
            "📋 *Режим выполнения заданий*\n\n"
            "Есть готовый отчёт другого студента?\n"
            "Пришли его — сделаю в том же стиле.\n\n"
            "_Или /skip чтобы пропустить_",
            parse_mode=ParseMode.MARKDOWN,
        )
        return WAITING_REFERENCE


# ─────────────────────────────────────────────────────────────────────────────
# Reference sample handler
# ─────────────────────────────────────────────────────────────────────────────

async def handle_reference_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """User sends a sample completed report as reference."""
    uid = update.effective_user.id
    session = store.get(uid)
    if not session:
        return ConversationHandler.END

    doc: Document = update.message.document
    if doc:
        fname = doc.file_name or ""
        if fname.endswith(".docx") or fname.endswith(".pdf"):
            cfg = get_config()
            downloads = cfg.log_dir / "downloads"
            downloads.mkdir(parents=True, exist_ok=True)
            ref_path = downloads / f"ref_{uuid.uuid4().hex}_{fname}"
            tg_file = await ctx.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(str(ref_path))
            session.reference_path = ref_path
            await update.message.reply_text(
                f"✅ Образец сохранён: *{fname}*\n"
                "Буду делать в том же стиле!\n\n"
                "Как тебя представить в отчёте?\n"
                "Введи ФИО студента (или /skip):",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                "Нужен .docx или .pdf файл. Попробуй ещё раз или /skip:"
            )
            return WAITING_REFERENCE
    # No file → treat as skip
    else:
        await update.message.reply_text(
            "Пропускаю образец.\n\n"
            "Как тебя представить в отчёте?\n"
            "Введи ФИО студента (или /skip):"
        )

    return WAITING_STUDENT


async def handle_reference_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """User typed /skip for reference step."""
    await update.message.reply_text(
        "Хорошо, без образца.\n\n"
        "Как тебя представить в отчёте?\n"
        "Введи ФИО студента (или /skip):"
    )
    return WAITING_STUDENT


# ─────────────────────────────────────────────────────────────────────────────
# Student info handlers
# ─────────────────────────────────────────────────────────────────────────────

async def handle_student(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    uid = update.effective_user.id
    session = store.get(uid)
    if not session:
        return ConversationHandler.END

    if text != "/skip":
        session.student_name = text

    await update.message.reply_text(
        "Введи номер группы (или /skip):"
    )
    return WAITING_GROUP


async def handle_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    uid = update.effective_user.id
    session = store.get(uid)
    if not session:
        return ConversationHandler.END

    if text != "/skip":
        session.group = text

    # Show confirmation
    info_lines = [f"📄 Файл: `{session.original_filename}`"]
    if session.student_name:
        info_lines.append(f"👤 Студент: {session.student_name}")
    if session.group:
        info_lines.append(f"🏫 Группа: {session.group}")

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Запустить", callback_data="run_confirm"),
            InlineKeyboardButton("❌ Отмена", callback_data="run_cancel"),
        ]
    ])

    await update.message.reply_text(
        "Готов к запуску:\n\n" + "\n".join(info_lines) + "\n\nНачать выполнение?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard,
    )
    return CONFIRM_RUN


# ─────────────────────────────────────────────────────────────────────────────
# Confirmation & execution
# ─────────────────────────────────────────────────────────────────────────────

async def handle_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    uid = update.effective_user.id

    if query.data == "run_cancel":
        store.remove(uid)
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    session = store.get(uid)
    if not session:
        await query.edit_message_text("Сессия устарела. Отправь файл заново.")
        return ConversationHandler.END

    await query.edit_message_text(
        "🚀 *Запускаю агент...*\n\n"
        "Агент откроет Word/Excel и начнёт выполнять задания.\n"
        "Я буду присылать обновления по ходу.\n\n"
        "_Это может занять несколько минут._",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Run in background — don't block the bot event loop
    runner = AgentRunner(
        bot=ctx.bot,
        chat_id=update.effective_chat.id,
        session=session,
    )
    asyncio.create_task(runner.run())

    return ConversationHandler.END


# ─────────────────────────────────────────────────────────────────────────────
# Build and run application
# ─────────────────────────────────────────────────────────────────────────────

def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            CommandHandler("run", cmd_start),
            MessageHandler(filters.Document.ALL, handle_file),
        ],
        states={
            WAITING_FILE: [
                MessageHandler(filters.Document.ALL, handle_file),
            ],
            WAITING_MODE: [
                CallbackQueryHandler(handle_mode, pattern="^mode_"),
            ],
            WAITING_REFERENCE: [
                MessageHandler(filters.Document.ALL, handle_reference_file),
                CommandHandler("skip", handle_reference_skip),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_reference_skip),
            ],
            WAITING_STUDENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_student),
                CommandHandler("skip", handle_student),
            ],
            WAITING_GROUP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_group),
                CommandHandler("skip", handle_group),
            ],
            CONFIRM_RUN: [
                CallbackQueryHandler(handle_confirm, pattern="^run_"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            # If user sends /start or /run mid-conversation → restart cleanly
            CommandHandler("start", cmd_cancel),
            CommandHandler("run", cmd_cancel),
        ],
        allow_reentry=False,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))

    return app


def main() -> None:
    import os
    from dotenv import load_dotenv

    load_dotenv()
    cfg = get_config()
    cfg.ensure_dirs()

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    # Logging
    logger.remove()
    logger.add(sys.stderr, level="INFO", colorize=True,
               format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")
    logger.add(str(cfg.log_dir / "telegram_bot.log"), level="DEBUG", rotation="20 MB")

    logger.info("Starting Telegram bot...")
    app = build_app(token)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
