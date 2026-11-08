"""Telegram setup helpers behind `sdr-loop telegram`: validate the token, find your chat id,
send a test message.

Telegram puts the bot token in the URL path, and httpx exceptions embed the URL. Every error
raised here is rewritten from scratch, so the token can never reach a terminal or a log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

API = "https://api.telegram.org"


class TelegramSetupError(Exception):
    """A user-facing problem. The message never contains the token."""


@dataclass(frozen=True)
class ChatInfo:
    chat_id: int
    label: str


async def _call(
    client: httpx.AsyncClient, token: str, method: str, payload: dict[str, Any] | None = None
) -> Any:
    try:
        r = await client.post(f"{API}/bot{token}/{method}", json=payload or {})
    except httpx.HTTPError:  # str(e) would contain the URL, and the URL contains the token
        raise TelegramSetupError(
            "Could not reach Telegram. Check your internet connection."
        ) from None
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code == 200 and data.get("ok"):
        return data.get("result")
    desc = str(data.get("description") or f"HTTP {r.status_code}")
    if r.status_code == 401:
        raise TelegramSetupError(
            f"Telegram rejected the token ({desc}). Copy it again from @BotFather."
        )
    if r.status_code == 409:
        raise TelegramSetupError(
            "Another process is already reading this bot's messages (is `sdr-loop bot` already "
            "running?). Stop it, or set TELEGRAM_CHAT_ID if you already know your chat id."
        )
    if r.status_code == 403:
        raise TelegramSetupError(
            f"Telegram refused to message that chat ({desc}). Open the bot in Telegram and press "
            "Start first, then retry."
        )
    raise TelegramSetupError(f"Telegram says: {desc}")


async def get_me(client: httpx.AsyncClient, token: str) -> str:
    me = await _call(client, token, "getMe")
    return f"@{me['username']}" if me.get("username") else str(me.get("first_name", "bot"))


async def find_chats(client: httpx.AsyncClient, token: str) -> list[ChatInfo]:
    """Private chats that have recently messaged the bot (these are where your chat id shows up)."""
    updates = await _call(client, token, "getUpdates", {"limit": 100, "timeout": 0})
    seen: dict[int, ChatInfo] = {}
    for u in updates or []:
        chat = (u.get("message") or {}).get("chat")
        if not chat or chat.get("type") != "private" or chat["id"] in seen:
            continue
        name = (
            " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or "unknown"
        )
        handle = f" (@{chat['username']})" if chat.get("username") else ""
        seen[chat["id"]] = ChatInfo(chat["id"], f"{name}{handle}")
    return list(seen.values())


async def send_test(client: httpx.AsyncClient, token: str, chat_id: int | str) -> None:
    await _call(
        client,
        token,
        "sendMessage",
        {"chat_id": int(chat_id), "text": "✅ sdr-loop is connected. Drafts will show up here."},
    )
