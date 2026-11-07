"""python-telegram-bot glue around BotController. Run with `sdr-loop bot`."""

from __future__ import annotations

import logging

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from sdr_loop.bot.controller import BotController, Reply

log = logging.getLogger(__name__)
PUSH_EVERY_S = 30
REMINDER_EVERY_S = 24 * 3600


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


def build_application(controller: BotController, token: str) -> Application:  # type: ignore[type-arg]
    app = Application.builder().token(token).build()

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

    async def push(context: ContextTypes.DEFAULT_TYPE) -> None:
        for _id, reply in await controller.push_pending():
            await context.bot.send_message(
                int(controller.settings.telegram_chat_id), reply.text, reply_markup=_markup(reply)
            )

    async def remind(context: ContextTypes.DEFAULT_TYPE) -> None:
        if (text := await controller.reminder()) is not None:
            await context.bot.send_message(int(controller.settings.telegram_chat_id), text)

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    if app.job_queue is not None:
        app.job_queue.run_repeating(push, interval=PUSH_EVERY_S, first=2)
        app.job_queue.run_repeating(remind, interval=REMINDER_EVERY_S, first=REMINDER_EVERY_S)
    else:
        log.warning("job queue unavailable: install python-telegram-bot[job-queue]")
    return app
