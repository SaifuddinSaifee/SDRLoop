"""sdr-loop command line.

Day to day you only need `sdr-loop start`: it checks and fixes setup, runs the Telegram bot and the
nightly learning cycle, and opens the control panel. The other commands are the same pieces, one
at a time (scripts, Docker, debugging).
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import threading
import time as _time
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Annotated

import httpx
import typer

from sdr_loop import health, llm
from sdr_loop.config import Settings, get_settings
from sdr_loop.demo import fake_gateway
from sdr_loop.evaboot import EvabootClient
from sdr_loop.memory import Repository
from sdr_loop.observability import init_tracer_from_settings
from sdr_loop.pipeline import Pipeline, RunReport, start_fixture_run
from sdr_loop.steps.extract import ExtractReport, run_live_extraction
from sdr_loop.steps.qualify import load_icp
from sdr_loop.tools.offline import offline_transport

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Self-improving SDR agent.")
prompts_app = typer.Typer(help="Prompt management.")
app.add_typer(prompts_app, name="prompts")

FIXTURE_CSV = Path("fixtures/evaboot_export.csv")


def _setup_logging() -> None:
    """INFO for our code. httpx/httpcore log full request URLs, which for Telegram contain the bot
    token (api.telegram.org/bot<TOKEN>/...), so they are held at WARNING: keys never reach logs."""
    logging.basicConfig(level=logging.INFO)
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _fail(msg: str) -> typer.Exit:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    return typer.Exit(1)


async def _open_repo(s: Settings) -> Repository:
    repo = Repository(s.database_url)
    if s.database_url.startswith("sqlite"):
        await repo.init_schema()  # local convenience; Postgres uses `alembic upgrade head`
    return repo


def _require_real_models(s: Settings) -> None:
    if not s.together_api_key:
        raise _fail("Missing TOGETHER_API_KEY in .env (or use --fake-llm for a canned demo).")


async def _always_public(_host: str) -> bool:
    return True


def _print_report(rep: RunReport, ext: ExtractReport | None) -> None:
    typer.echo(f"\nRun {rep.run_id}: {rep.status}")
    if ext:
        skipped = ", ".join(f"{k}={v}" for k, v in sorted(ext.skipped.items())) or "none"
        typer.echo(
            f"  extract: {ext.total} rows, {ext.new} new leads, {ext.duplicates} duplicates, "
            f"skipped: {skipped}"
        )
    typer.echo(
        "  leads:   " + (", ".join(f"{k}={v}" for k, v in sorted(rep.counts.items())) or "none")
    )
    cpq = rep.cost_per_qualified_lead
    p95 = rep.p95_latency_s
    typer.echo(
        f"  cost:    ${rep.cost_usd:.4f} total"
        + (f", ${cpq:.4f} per qualified lead" if cpq is not None else "")
        + (f"; p95 {p95:.1f}s per lead" if p95 is not None else "")
    )
    pending = rep.counts.get("pending_approval", 0)
    if pending:
        typer.echo(
            f"  {pending} draft(s) await approval in the control panel and on Telegram "
            "(while `sdr-loop start` is running)."
        )


@app.command()
def run(
    url: Annotated[str | None, typer.Argument(help="Sales Navigator search URL")] = None,
    fixture: Annotated[
        bool, typer.Option(help="Use the recorded CSV export; no Evaboot call")
    ] = False,
    fake_llm: Annotated[
        bool, typer.Option(help="Canned model outputs (demo, no API keys)")
    ] = False,
    limit: Annotated[int | None, typer.Option(help="Process at most N leads")] = None,
    resume: Annotated[int | None, typer.Option(help="Resume an existing run id")] = None,
    icp_path: Annotated[Path, typer.Option("--icp")] = Path("icp.yaml"),
) -> None:
    """Extract, qualify, research, write and critique; drafts then await approval on Telegram."""
    _setup_logging()
    s = get_settings()
    if not s.dry_run:
        raise _fail("DRY_RUN=false: v1 has no sending adapters. Refusing to start.")
    if not (url or fixture or resume):
        raise _fail("Give a Sales Navigator URL, --fixture, or --resume RUN_ID.")
    if fake_llm:
        llm.set_gateway(fake_gateway(s))
        typer.secho("fake-llm: canned outputs, not real model quality.", fg=typer.colors.YELLOW)
    else:
        _require_real_models(s)
    asyncio.run(_run(s, url, fixture, limit, resume, icp_path))


async def _run(
    s: Settings,
    url: str | None,
    fixture: bool,
    limit: int | None,
    resume: int | None,
    icp_path: Path,
) -> None:
    tracer = init_tracer_from_settings()
    repo = await _open_repo(s)
    icp, icp_version = load_icp(icp_path)
    offline = fixture or s.offline
    http = httpx.AsyncClient(transport=offline_transport()) if offline else httpx.AsyncClient()
    ext: ExtractReport | None = None
    try:
        if resume:
            run_id = resume
        elif fixture:
            run_id, ext = await start_fixture_run(repo, FIXTURE_CSV, icp_version)
        else:
            assert url
            simulated = s.evaboot_mode == "simulated"
            if not (simulated or s.evaboot_api_key):
                raise _fail(
                    "EVABOOT_API_KEY is not set. Use --fixture to run on the recorded export."
                )
            run_id = await repo.create_run(url, icp_version)
            hook = (
                f"{s.public_webhook_url.rstrip('/')}/webhooks/evaboot/{s.evaboot_webhook_secret}/{run_id}"
                if s.public_webhook_url and not simulated
                else None
            )
            if simulated:
                from sdr_loop.evaboot import BASE_URL
                from sdr_loop.evaboot_sim import EvabootSimulator

                sim = httpx.AsyncClient(transport=EvabootSimulator().transport(), base_url=BASE_URL)
                client = EvabootClient("simulated", client=sim)
                typer.echo("Evaboot: simulated (demo export of real companies).")
            else:
                client = EvabootClient(s.evaboot_api_key)
            await run_live_extraction(
                repo, client, run_id, url, hook, poll_s=3.0 if simulated else 5.0
            )
            run = await repo.get_run(run_id)
            if run is None or run.status != "extracted":
                raise _fail(f"Extraction failed: {run.failure_reason if run else 'unknown'}")
        # Offline fixtures use fake .example domains: skip the real-DNS "is this host public" check.
        report = await Pipeline(
            repo, s, icp, icp_version, http, _always_public if offline else None
        ).run(run_id, limit)
        _print_report(report, ext)
    finally:
        tracer.flush()
        await http.aclose()
        await repo.close()


UI_APP = Path(__file__).parent / "ui" / "streamlit_app.py"
ICONS = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}


def _print_checks(prep: health.Prepared) -> None:
    typer.echo("\nSetup")
    for c in prep.checks:
        typer.echo(f"  {ICONS[c.status]} {c.name}: {c.detail}")
        if c.fix and c.status != "ok":
            typer.echo(f"       fix: {c.fix}")
    for a in prep.actions:
        typer.secho(f"  🔧 {a}", fg=typer.colors.CYAN)


def _learn_time(s: Settings) -> time | None:
    """LEARN_AT (HH:MM) in this machine's local time zone; None when learning is off."""
    if not s.learn_enabled:
        return None
    hh, mm = (int(x) for x in s.learn_at.split(":"))
    return time(hh, mm, tzinfo=datetime.now().astimezone().tzinfo)


def _run_bot(s: Settings) -> None:
    from sdr_loop.bot.controller import BotController
    from sdr_loop.bot.telegram import build_application
    from sdr_loop.jobs import JobStore

    init_tracer_from_settings()
    repo = Repository(s.database_url)
    controller = BotController(repo, s, JobStore(s.state_dir))
    asyncio.set_event_loop(asyncio.new_event_loop())  # a fresh loop after earlier asyncio.run()s
    build_application(controller, s.telegram_bot_token).run_polling()


TELEGRAM_POLL_S = 5


def _wait_for_telegram(ui: subprocess.Popen[bytes]) -> Settings | None:
    """Wait until Telegram is usable: a valid token (from .env or the Setup page) and your chat
    id (found as soon as you message the bot). None if the control panel exits first."""
    from sdr_loop.bot.setup import TelegramSetupError, find_chats, get_me
    from sdr_loop.envfile import save_setting

    said: set[str] = set()

    def say(key: str, text: str) -> None:
        if key not in said:
            said.add(key)
            typer.secho(text, fg=typer.colors.YELLOW)

    async def probe(s: Settings) -> tuple[str, str | None]:
        async with _telegram_client() as client:
            handle = await get_me(client, s.telegram_bot_token)
            if s.telegram_chat_id:
                return handle, s.telegram_chat_id
            chats = await find_chats(client, s.telegram_bot_token)
        return handle, str(chats[0].chat_id) if chats else None

    while ui.poll() is None:
        get_settings.cache_clear()
        s = get_settings()
        if not s.telegram_bot_token:
            say("off", "Telegram is off (add a bot token in Setup to turn it on). Ctrl-C to stop.")
        else:
            try:
                handle, chat = asyncio.run(probe(s))
            except TelegramSetupError as e:
                say(f"err:{e}", f"Telegram: {e}")
            else:
                if chat is None:
                    say(handle, f"\n👉 Open https://t.me/{handle.lstrip('@')} in Telegram, "
                                "press Start and send 'hi'. Waiting…")  # fmt: skip
                else:
                    if not s.telegram_chat_id:
                        save_setting("TELEGRAM_CHAT_ID", chat)
                        typer.secho(f"  🔧 Found your chat ({chat}) and saved it to .env.",
                                    fg=typer.colors.CYAN)  # fmt: skip
                        get_settings.cache_clear()
                    return get_settings()
        _time.sleep(TELEGRAM_POLL_S)
    return None


def _next_learn(s: Settings, after: datetime) -> datetime | None:
    t = _learn_time(s)
    if t is None:
        return None
    nxt = datetime.combine(after.date(), t)
    return nxt if nxt > after else nxt + timedelta(days=1)


def _background_tick(s: Settings, now: datetime, next_learn: datetime | None) -> datetime | None:
    """One pass: send due demo emails; start the nightly learning cycle when it's time."""
    from sdr_loop.jobs import JobBusy, JobStore
    from sdr_loop.steps.send import dispatch_due

    if s.email_mode == "mailpit":

        async def send() -> list[str]:
            repo = await _open_repo(s)
            try:
                return await dispatch_due(repo, s)
            finally:
                await repo.close()

        for line in asyncio.run(send()):
            logging.getLogger("sdr_loop.send").info("sent %s to the demo inbox", line)
    if next_learn is not None and now >= next_learn:
        try:
            JobStore(s.state_dir).start(
                "learn", ["learn"], label="Nightly learning cycle", source="schedule"
            )
        except JobBusy:
            pass
        return _next_learn(s, now)
    return next_learn


def _background_loop(stop: threading.Event) -> None:
    get_settings.cache_clear()
    next_learn = _next_learn(get_settings(), datetime.now().astimezone())
    while not stop.wait(BACKGROUND_EVERY_S):
        get_settings.cache_clear()  # pick up changes made on the Setup page
        try:
            next_learn = _background_tick(get_settings(), datetime.now().astimezone(), next_learn)
        except Exception:  # noqa: BLE001  (keep the loop alive; the error is logged)
            logging.getLogger(__name__).exception("background tick failed")


BACKGROUND_EVERY_S = 10


def _spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(argv)  # noqa: S603  (fixed argv, no shell)


@app.command()
def start(
    port: Annotated[int, typer.Option(help="Control panel port")] = 8501,
    host: Annotated[str, typer.Option(help="Control panel address (0.0.0.0 in Docker)")] = (
        "localhost"
    ),
    browser: Annotated[bool, typer.Option(help="Open the control panel in a browser")] = True,
    webhook_api: Annotated[
        bool, typer.Option("--api/--no-api", help="Also serve the Evaboot webhook if configured")
    ] = True,
) -> None:
    """Start everything: setup checks, control panel, Telegram bot and nightly learning."""
    _setup_logging()
    s = get_settings()
    if not s.dry_run:
        raise _fail("DRY_RUN=false: v1 has no sending adapters. Refusing to start.")
    prep = asyncio.run(health.prepare(s))
    _print_checks(prep)
    get_settings.cache_clear()
    s = get_settings()  # prepare() may have saved the chat id

    procs = [
        _spawn(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(UI_APP),
                "--server.port",
                str(port),
                "--server.address",
                host,
                "--server.headless",
                "false" if browser else "true",
                "--browser.gatherUsageStats",
                "false",
            ]
        )  # fmt: skip
    ]
    if (
        webhook_api and s.public_webhook_url
    ):  # live Evaboot runs can be pushed to us instead of polled
        procs.append(_spawn([sys.executable, "-m", "sdr_loop.cli", "api"]))
    typer.secho(f"\n🖥  Control panel: http://{host}:{port}", fg=typer.colors.GREEN, bold=True)
    stop = threading.Event()
    threading.Thread(target=_background_loop, args=(stop,), daemon=True).start()
    if s.learn_enabled:
        typer.echo(f"🧠 Nightly learning at {s.learn_at}.")
    if s.email_mode == "mailpit":
        typer.echo(f"📬 Approved emails go to the demo inbox: {s.mailpit_ui_url or s.mailpit_url}")
    try:
        ready = _wait_for_telegram(procs[0])
        if ready is not None:
            typer.secho(
                "📱 Telegram bot is on: drafts arrive as they're ready; send /help to the bot."
                "\nCtrl-C to stop.",
                fg=typer.colors.GREEN,
            )
            _run_bot(ready)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


@app.command()
def check() -> None:
    """Check setup (models, Telegram, Langfuse, database) and fix what needs no human."""
    _setup_logging()
    prep = asyncio.run(health.prepare(get_settings()))
    _print_checks(prep)
    if any(c.status == "fail" for c in prep.checks):
        raise typer.Exit(1)


@app.command()
def bot() -> None:
    """Run only the Telegram bot (`sdr-loop start` runs it for you)."""
    _setup_logging()
    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        raise _fail(
            "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (or run `sdr-loop start`)."
        )
    _run_bot(s)


_TG_TIMEOUT_S = 15.0


def _telegram_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TG_TIMEOUT_S)


@app.command()
def telegram() -> None:
    """Check the Telegram bot: validate the token, find your chat id, send a test message."""
    from sdr_loop.bot.setup import TelegramSetupError, find_chats, get_me, send_test

    _setup_logging()
    s = get_settings()
    if not s.telegram_bot_token:
        raise _fail(
            "TELEGRAM_BOT_TOKEN is empty. In Telegram, message @BotFather, send /newbot, "
            "and paste the token it gives you into .env."
        )

    async def go() -> None:
        async with _telegram_client() as client:
            me = await get_me(client, s.telegram_bot_token)
            typer.echo(f"Token OK: your bot is {me}")
            if s.telegram_chat_id:
                await send_test(client, s.telegram_bot_token, s.telegram_chat_id)
                typer.echo(f"Test message sent to chat {s.telegram_chat_id}. Check your phone.")
                return
            chats = await find_chats(client, s.telegram_bot_token)
            if not chats:
                handle = me.lstrip("@")
                raise _fail(
                    f"No messages found yet. Open https://t.me/{handle} in Telegram, press Start, "
                    "send 'hi', then run this command again."
                )
            for c in chats:
                typer.echo(f"  chat id {c.chat_id}: {c.label}")
            typer.echo(
                f"\nAdd this line to .env, then run this command again to test it:\n"
                f"TELEGRAM_CHAT_ID={chats[0].chat_id}"
            )

    try:
        asyncio.run(go())
    except TelegramSetupError as e:
        raise _fail(str(e)) from None


@app.command()
def api(host: str = "0.0.0.0", port: int = 8000) -> None:  # noqa: S104
    """Run the Evaboot webhook receiver."""
    import uvicorn

    from sdr_loop.api.webhooks import create_app

    s = get_settings()
    uvicorn.run(create_app(Repository(s.database_url), s), host=host, port=port)


@app.command()
def learn() -> None:
    """One learning cycle: edits/rejections -> challenger -> champion vs challenger -> promote?"""
    from sdr_loop.learn.cycle import run_learning_cycle

    _setup_logging()
    s = get_settings()
    _require_real_models(s)
    tracer = init_tracer_from_settings()

    async def go() -> None:
        repo = await _open_repo(s)
        try:
            res = await run_learning_cycle(repo, s)
        finally:
            tracer.flush()
            await repo.close()
        typer.echo(f"learn: {res.outcome}" + (f" ({res.reason})" if res.reason else ""))
        if res.detail:
            d = res.detail
            typer.echo(
                f"  champion {d['champion_avg']:.3f} vs challenger {d['challenger_avg']:.3f}; "
                f"invalid citations {d['challenger_invalid_citations']}; "
                f"regressions {d['regression_rate']:.0%}"
            )

    asyncio.run(go())


@app.command()
def purge(
    lead: Annotated[str, typer.Option("--lead", help="Lead id or LinkedIn profile URL")],
) -> None:
    """Delete a lead, its facts, drafts, decisions, eval cases, outbox lines and trace."""
    from sdr_loop.observability import get_tracer
    from sdr_loop.privacy import purge_lead

    s = get_settings()
    init_tracer_from_settings()

    async def go() -> None:
        repo = await _open_repo(s)
        try:
            rep = await purge_lead(repo, get_tracer(), lead, s.outbox_path)
        finally:
            await repo.close()
        if not rep.found:
            raise _fail(f"No lead matches {lead!r}.")
        typer.echo(
            f"purged: {rep.rows}; outbox lines removed: {rep.outbox_lines_removed}; "
            f"trace deleted: {rep.trace_deleted}"
        )

    asyncio.run(go())


@prompts_app.command("push")
def prompts_push() -> None:
    """Seed Langfuse with the local prompt files, labelled `production`."""
    from sdr_loop.prompts import push_local_prompts

    s = get_settings()
    if not (s.langfuse_public_key and s.langfuse_secret_key):
        raise _fail("Set LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY in .env.")
    for ref in push_local_prompts():
        typer.echo(f"pushed {ref}")


if __name__ == "__main__":
    app()
