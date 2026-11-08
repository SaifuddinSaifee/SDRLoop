"""Setup checks and automatic fixes, shared by `sdr-loop start` and the web app's Home page.

Each check says what is wrong in plain words and what fixes it. `prepare()` applies every fix that
needs no human: create the local database, save the Telegram chat id once you have messaged the
bot, and seed missing prompts in Langfuse. Nothing here prints or logs a key.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from sdr_loop import mailer
from sdr_loop.bot.setup import TelegramSetupError, find_chats, get_me
from sdr_loop.campaign import load_campaign
from sdr_loop.config import Settings
from sdr_loop.envfile import ENV_PATH, set_env_value
from sdr_loop.memory import Repository

Status = Literal["ok", "warn", "fail"]
TOGETHER_MODELS_URL = "https://api.together.ai/v1/models"
EVABOOT_API = "https://api.evaboot.com"


@dataclass(frozen=True)
class Check:
    key: str
    name: str
    status: Status
    detail: str
    fix: str = ""
    action: str = ""  # an automatic fix prepare() / the Setup page can apply


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
    from sdr_loop.prompts import prompts_to_upload

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
    todo = prompts_to_upload(lf)
    if todo:
        return Check(
            "langfuse", name, "warn", f"Connected; prompts to upload: {', '.join(todo)}.",
            "Click 'Upload prompts' (or it happens on the next `sdr-loop start`).",
            action="upload_prompts",
        )  # fmt: skip
    return Check("langfuse", name, "ok", f"Connected to {s.langfuse_host}; prompts in place")


async def check_evaboot(s: Settings, client: httpx.AsyncClient) -> Check:
    name = "Evaboot (leads from Sales Navigator)"
    searches = load_campaign()[0].search_urls
    if s.evaboot_mode == "simulated":
        if not searches:
            return Check(
                "evaboot", name, "warn", "Simulated, but no Sales Navigator search saved yet.",
                "Setup → Leads: paste a Sales Navigator search URL.",
            )  # fmt: skip
        return Check(
            "evaboot", name, "ok",
            f"Simulated Evaboot (demo export of real companies); {len(searches)} search(es) saved",
        )  # fmt: skip
    if not s.evaboot_api_key:
        return Check(
            "evaboot", name, "warn", "No key: runs use the recorded export (fixture) only.",
            "Setup → Leads: paste your Evaboot API key.",
        )  # fmt: skip
    try:
        r = await client.get(
            f"{EVABOOT_API}/v1/quota/", headers={"Authorization": f"Bearer {s.evaboot_api_key}"}
        )
    except httpx.HTTPError:
        r = None
    if r is not None and r.status_code in (401, 403):
        return Check("evaboot", name, "fail", "Evaboot rejected the API key.",
                     "Copy it again from Evaboot → Integrations → API.")  # fmt: skip
    quota = (r.json().get("quota") or {}) if r is not None and r.status_code == 200 else {}
    if quota and not quota.get("has_valid_salesnav"):
        return Check(
            "evaboot", name, "warn",
            f"Key works, but no Sales Navigator seat is connected to Evaboot "
            f"(credits: {quota.get('credits', 0):g}). Live extraction can't run.",
            "Setup → Leads: switch to Simulated for a demo, or connect a Sales Navigator seat.",
        )  # fmt: skip
    if not searches:
        return Check(
            "evaboot", name, "warn", "Key set, but no Sales Navigator search saved yet.",
            "Setup → Leads: paste a Sales Navigator search URL.",
        )  # fmt: skip
    how = "webhook" if s.public_webhook_url else "polling"
    credits = f"{quota['credits']:g} credits; " if "credits" in quota else ""
    return Check("evaboot", name, "ok", f"Live; {credits}{len(searches)} search(es) saved; {how}")


def check_campaign() -> Check:
    name = "Company and offer (campaign brief)"
    c, _ = load_campaign()
    if missing := c.missing():
        return Check(
            "campaign", name, "fail", f"Missing: {', '.join(missing)}.",
            "Setup → Company & offer: fill these in so the writer knows who it writes for.",
        )  # fmt: skip
    return Check("campaign", name, "ok", f"{c.company}, signed by {c.sender.name}")


async def check_email(s: Settings, client: httpx.AsyncClient) -> Check:
    name = "Email delivery (demo inbox)"
    if s.email_mode != "mailpit":
        return Check(
            "email", name, "warn", "Off: approved emails go to outbox.jsonl only.",
            "Setup → Email delivery: turn on 'Send approved emails to the demo inbox'.",
        )  # fmt: skip
    version = await mailer.mailpit_version(s, client)
    if version is None:
        return Check(
            "email", name, "fail", f"Mailpit is not reachable at {s.mailpit_url}.",
            "Setup → Email delivery: click 'Start Mailpit' (needs Docker), or run "
            "`docker compose up -d mailpit`.",
            action="start_mailpit",
        )  # fmt: skip
    return Check("email", name, "ok", f"Mailpit {version}: inbox at {mailer.inbox_url(s)}")


def start_mailpit() -> tuple[bool, str]:
    """`docker compose up -d mailpit` from the project directory."""
    try:
        r = subprocess.run(  # noqa: S603, S607  (fixed argv)
            ["docker", "compose", "up", "-d", "mailpit"],
            capture_output=True, text=True, timeout=180,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"Could not run Docker: {type(e).__name__}"
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip().splitlines()[-1][:300]
    return True, "Mailpit started"


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
        db, together, (tg, handle, chat), lf, email, evaboot = await asyncio.gather(
            check_database(s),
            check_together(s, http),
            check_telegram(s, http, discover=discover_chat),
            asyncio.to_thread(check_langfuse, s),
            check_email(s, http),
            check_evaboot(s, http),
        )
    finally:
        if own:
            await http.aclose()
    return [check_campaign(), together, evaboot, email, tg, lf, db], handle, chat


async def prepare(
    s: Settings, *, env_path: Path | None = None, client: httpx.AsyncClient | None = None
) -> Prepared:
    """Run every check and apply the fixes that need no human."""
    checks, handle, chat = await run_checks(s, discover_chat=True, client=client)
    out = Prepared(checks, bot_handle=handle)
    if chat:
        set_env_value("TELEGRAM_CHAT_ID", chat, env_path)
        os.environ["TELEGRAM_CHAT_ID"] = chat  # an empty env var would otherwise win over .env
        out.actions.append(f"Saved your Telegram chat id ({chat}) to {env_path or ENV_PATH}.")
    em = out.get("email")
    if em.action == "start_mailpit":
        ok, msg = await asyncio.to_thread(start_mailpit)
        out.actions.append(msg if ok else f"Could not start Mailpit: {msg}")
        if ok:
            for _ in range(20):  # it takes a moment to accept connections
                async with httpx.AsyncClient(timeout=3) as http:
                    if (fresh := await check_email(s, http)).status == "ok":
                        out.checks[out.checks.index(em)] = fresh
                        break
                await asyncio.sleep(0.5)
    lf = out.get("langfuse")
    if lf.action == "upload_prompts":
        from sdr_loop.prompts import prompts_to_upload, push_local_prompts

        pushed = await asyncio.to_thread(lambda: push_local_prompts(prompts_to_upload()))
        out.actions.append(f"Uploaded prompts to Langfuse: {', '.join(pushed)}.")
        out.checks[out.checks.index(lf)] = Check(
            lf.key, lf.name, "ok", f"Connected to {s.langfuse_host}; prompts in place"
        )
    return out
