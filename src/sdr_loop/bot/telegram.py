"""python-telegram-bot glue around BotController. Started by `sdr-loop start` or `sdr-loop bot`."""

from __future__ import annotations

import logging
from datetime import time

from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from sdr_loop.bot.controller import BotController, Reply
from sdr_loop.jobs import JobBusy

log = logging.getLogger(__name__)
PUSH_EVERY_S = 5  # drafts and job results show up within seconds of being ready
REMINDER_EVERY_S = 24 * 3600
COMMANDS = [
    BotCommand("run", "Process leads: /run, /run 20, or /run <Sales Navigator URL> 20"),
    BotCommand("status", "What's waiting for review and what's running"),
    BotCommand("learn", "Run a learning cycle now"),
    BotCommand("help", "How this bot works"),
]


def _markup(reply: Reply) -> InlineKeyboardMarkup | ForceReply | None:
    if reply.force_reply:
        return ForceReply(selective=True)
    if reply.buttons:
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(label, callback_data=data) for label, data in row]
                for row in reply.buttons
            ]
        )
    return None


def build_application(
    controller: BotController, token: str, *, learn_at: time | None = None
) -> Application:  # type: ignore[type-arg]
    """`learn_at` schedules the nightly learning cycle (needs the controller's job store)."""

    async def post_init(application: Application) -> None:  # type: ignore[type-arg]
        await application.bot.set_my_commands(COMMANDS)

    app = Application.builder().token(token).post_init(post_init).build()

    async def on_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        if q is None or q.message is None or q.data is None:
            return
        reply = await controller.on_action(q.message.chat.id, q.data)
        await q.answer()
        if reply is not None:
            await q.message.chat.send_message(reply.text, reply_markup=_markup(reply))

    async def on_message(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None or not update.message.text:
            return
        reply = await controller.on_text(update.effective_chat.id, update.message.text)
        if reply is not None:
            await update.message.reply_text(reply.text, reply_markup=_markup(reply))

    async def on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_chat is None or update.message is None or not update.message.text:
            return
        command = update.message.text.split()[0].lstrip("/").split("@")[0].lower()
        reply = await controller.on_command(update.effective_chat.id, command, context.args or [])
        if reply is not None:
            await update.message.reply_text(reply.text, reply_markup=_markup(reply))

    async def push(context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = int(controller.settings.telegram_chat_id)
        for text in controller.announcements():
            await context.bot.send_message(chat, text)
        for _id, reply in await controller.push_pending():
            await context.bot.send_message(chat, reply.text, reply_markup=_markup(reply))

    async def nightly_learn(_: ContextTypes.DEFAULT_TYPE) -> None:
        assert controller.jobs is not None
        try:
            controller.jobs.start("learn", ["learn"], label="Nightly learning cycle",
                                  source="schedule")  # fmt: skip
        except JobBusy as e:
            log.info("nightly learn skipped: %s", e)

    async def remind(context: ContextTypes.DEFAULT_TYPE) -> None:
        if (text := await controller.reminder()) is not None:
            await context.bot.send_message(int(controller.settings.telegram_chat_id), text)

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(CommandHandler(["run", "learn", "status", "help", "start"], on_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    if app.job_queue is not None:
        app.job_queue.run_repeating(push, interval=PUSH_EVERY_S, first=2)
        app.job_queue.run_repeating(remind, interval=REMINDER_EVERY_S, first=REMINDER_EVERY_S)
        if learn_at is not None and controller.jobs is not None:
            app.job_queue.run_daily(nightly_learn, time=learn_at)
    else:
        log.warning("job queue unavailable: install python-telegram-bot[job-queue]")
    return app
