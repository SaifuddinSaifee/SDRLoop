"""Setup checks and automatic fixes, shared by `sdr-loop start` and the web app's Home page.

Each check says what is wrong in plain words and what fixes it. `prepare()` applies every fix that
needs no human: create the local database, save the Telegram chat id once you have messaged the
bot, and seed missing prompts in Langfuse. Nothing here prints or logs a key.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from sdr_loop.bot.setup import TelegramSetupError, find_chats, get_me
from sdr_loop.config import Settings
from sdr_loop.envfile import ENV_PATH, set_env_value
from sdr_loop.memory import Repository

Status = Literal["ok", "warn", "fail"]
TOGETHER_MODELS_URL = "https://api.together.ai/v1/models"


@dataclass(frozen=True)
class Check:
    key: str
    name: str
    status: Status
    detail: str
    fix: str = ""


@dataclass
class Prepared:
    checks: list[Check]
    actions: list[str] = field(default_factory=list)
    bot_handle: str | None = None

    def get(self, key: str) -> Check:
        return next(c for c in self.checks if c.key == key)


async def check_database(s: Settings) -> Check:
    repo = Repository(s.database_url)
    try:
        if s.database_url.startswith("sqlite"):
            await repo.init_schema()
        n = await repo.count_leads()
    except Exception as e:  # noqa: BLE001  (any failure is reported, never raised)
        fix = (
            "Start Postgres (`docker compose up -d postgres`), then `uv run alembic upgrade head`."
            if s.database_url.startswith("postgres")
            else "Check DATABASE_URL in .env."
        )
        return Check("database", "Database", "fail", f"Cannot open it: {type(e).__name__}", fix)
    finally:
        await repo.close()
    return Check("database", "Database", "ok", f"{n} leads stored")


async def check_together(s: Settings, client: httpx.AsyncClient) -> Check:
    name = "Together AI (models)"
    if not s.together_api_key:
        return Check(
            "together", name, "fail", "TOGETHER_API_KEY is missing: only demo runs (no AI) work.",
            "Create a key at api.together.ai and add TOGETHER_API_KEY=... to .env.",
        )  # fmt: skip
    try:
        r = await client.get(
            TOGETHER_MODELS_URL, headers={"Authorization": f"Bearer {s.together_api_key}"}
        )
    except httpx.HTTPError:
        return Check("together", name, "warn", "Could not reach Together AI to verify the key.")
    if r.status_code in (401, 403):
        return Check(
            "together", name, "fail", "Together AI rejected the key.",
            "Copy the key again from api.together.ai into TOGETHER_API_KEY in .env.",
        )  # fmt: skip
    if r.status_code != 200:
        return Check("together", name, "warn", f"Together AI answered HTTP {r.status_code}.")
    return Check("together", name, "ok", "Key works")


async def check_telegram(
    s: Settings, client: httpx.AsyncClient, *, discover: bool = False
) -> tuple[Check, str | None, str | None]:
    """(check, bot handle, discovered chat id). `discover` reads recent messages to find your chat
    id; only do that when no bot is polling, or Telegram answers 409."""
    name = "Telegram (approvals on your phone)"
    if not s.telegram_bot_token:
        return (
            Check(
                "telegram", name, "warn", "Not set up: review drafts in this web app instead.",
                "In Telegram, message @BotFather, send /newbot, and put the token in .env as "
                "TELEGRAM_BOT_TOKEN=...",
            ),
            None, None,
        )  # fmt: skip
    try:
        handle = await get_me(client, s.telegram_bot_token)
    except TelegramSetupError as e:
        return Check("telegram", name, "fail", str(e)), None, None
    link = f"https://t.me/{handle.lstrip('@')}"
    if s.telegram_chat_id:
        return (
            Check("telegram", name, "ok", f"Bot {handle} → chat {s.telegram_chat_id}"),
            handle,
            None,
        )
    found: str | None = None
    if discover:
        try:
            chats = await find_chats(client, s.telegram_bot_token)
        except TelegramSetupError:
            chats = []
        if len(chats) == 1:
            found = str(chats[0].chat_id)
    if found:
        return Check("telegram", name, "ok", f"Bot {handle} → chat {found}"), handle, found
    return (
        Check(
            "telegram", name, "warn", f"Bot {handle} works but doesn't know your chat yet.",
            f"Open {link} in Telegram, press Start and send 'hi'. It's picked up automatically.",
        ),
        handle, None,
    )  # fmt: skip


def check_langfuse(s: Settings, client_factory: Callable[[], Any] | None = None) -> Check:
    """Sync (the Langfuse SDK is sync): call it via asyncio.to_thread from async code."""
    name = "Langfuse (traces and prompts)"
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        return Check(
            "langfuse", name, "warn", "Not set up: runs work, but nothing is traced.",
            "Add LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY and LANGFUSE_HOST to .env.",
        )  # fmt: skip
    from sdr_loop.prompts import missing_prompts

    try:
        lf = client_factory() if client_factory else _langfuse(s)
        if not lf.auth_check():
            raise PermissionError
    except Exception:  # noqa: BLE001
        return Check(
            "langfuse", name, "fail", f"Keys rejected by {s.langfuse_host}.",
            "Check the keys, and that LANGFUSE_HOST matches your region (EU cloud.langfuse.com, "
            "US us.cloud.langfuse.com, JP jp.cloud.langfuse.com).",
        )  # fmt: skip
    missing = missing_prompts(lf)
    if missing:
        return Check(
            "langfuse", name, "warn", f"Connected, but prompts missing: {', '.join(missing)}.",
            "Click 'Upload prompts' (or it happens on the next `sdr-loop start`).",
        )  # fmt: skip
    return Check("langfuse", name, "ok", f"Connected to {s.langfuse_host}; prompts in place")


def check_evaboot(s: Settings) -> Check:
    name = "Evaboot (live Sales Navigator extraction)"
    if not s.evaboot_api_key:
        return Check(
            "evaboot", name, "warn", "No key: runs use the recorded export (fixture) only.",
            "Add EVABOOT_API_KEY to .env to run on a live Sales Navigator search.",
        )  # fmt: skip
    if not s.public_webhook_url:
        return Check("evaboot", name, "ok", "Key set; results are fetched by polling")
    return Check("evaboot", name, "ok", "Key set; webhook enabled")


_lf_clients: dict[tuple[str, str, str], Any] = {}


def _langfuse(s: Settings) -> Any:
    """One client per key set: the web app re-checks often, and keys can change in .env."""
    from langfuse import Langfuse

    k = (s.langfuse_public_key, s.langfuse_secret_key, s.langfuse_host)
    if k not in _lf_clients:
        _lf_clients[k] = Langfuse(public_key=k[0], secret_key=k[1], host=k[2])
    return _lf_clients[k]


async def run_checks(
    s: Settings, *, discover_chat: bool = False, client: httpx.AsyncClient | None = None
) -> tuple[list[Check], str | None, str | None]:
    own = client is None
    http = client or httpx.AsyncClient(timeout=10)
    try:
        db, together, (tg, handle, chat), lf = await asyncio.gather(
            check_database(s),
            check_together(s, http),
            check_telegram(s, http, discover=discover_chat),
            asyncio.to_thread(check_langfuse, s),
        )
    finally:
        if own:
            await http.aclose()
    return [db, together, tg, lf, check_evaboot(s)], handle, chat


async def prepare(
    s: Settings, *, env_path: Path = ENV_PATH, client: httpx.AsyncClient | None = None
) -> Prepared:
    """Run every check and apply the fixes that need no human."""
    checks, handle, chat = await run_checks(s, discover_chat=True, client=client)
    out = Prepared(checks, bot_handle=handle)
    if chat:
        set_env_value("TELEGRAM_CHAT_ID", chat, env_path)
        os.environ["TELEGRAM_CHAT_ID"] = chat  # an empty env var would otherwise win over .env
        out.actions.append(f"Saved your Telegram chat id ({chat}) to {env_path}.")
    lf = out.get("langfuse")
    if lf.status == "warn" and "prompts missing" in lf.detail:
        from sdr_loop.prompts import missing_prompts, push_local_prompts

        pushed = await asyncio.to_thread(lambda: push_local_prompts(missing_prompts()))
        out.actions.append(f"Uploaded prompts to Langfuse: {', '.join(pushed)}.")
        out.checks[out.checks.index(lf)] = Check(
            lf.key, lf.name, "ok", f"Connected to {s.langfuse_host}; prompts in place"
        )
    return out
