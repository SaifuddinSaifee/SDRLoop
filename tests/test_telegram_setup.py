"""`sdr-loop telegram`: validate the token, discover the chat id, send a test message."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from sdr_loop import cli
from sdr_loop.bot.setup import TelegramSetupError, find_chats, get_me, send_test
from sdr_loop.config import get_settings

TOKEN = "123456789:AAFake-Token_value"
runner = CliRunner()


def client(handler: Any) -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    return httpx.AsyncClient(transport=httpx.MockTransport(wrapped)), seen


def tg(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def update(chat_id: int, kind: str = "private", **chat: Any) -> dict[str, Any]:
    return {
        "update_id": chat_id,
        "message": {"text": "hi", "chat": {"id": chat_id, "type": kind, **chat}},
    }


async def test_get_me_returns_the_bot_username() -> None:
    c, seen = client(lambda r: tg({"id": 1, "username": "sdr_loop_bot", "first_name": "SDR"}))
    assert await get_me(c, TOKEN) == "@sdr_loop_bot"
    assert str(seen[0].url) == f"https://api.telegram.org/bot{TOKEN}/getMe"


async def test_bad_token_error_never_contains_the_token() -> None:
    c, _ = client(lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
    with pytest.raises(TelegramSetupError) as e:
        await get_me(c, TOKEN)
    assert (
        "Unauthorized" in str(e.value)
        and TOKEN not in str(e.value)
        and "AAFake" not in str(e.value)
    )


async def test_network_error_never_leaks_the_token_via_the_url() -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=req)

    c, _ = client(boom)
    with pytest.raises(TelegramSetupError) as e:
        await get_me(c, TOKEN)
    assert TOKEN not in str(e.value) and "AAFake" not in str(e.value)


async def test_find_chats_lists_unique_private_chats_only() -> None:
    updates = [
        update(42, first_name="Saif", username="saif"),
        update(42, first_name="Saif", username="saif"),  # same chat twice
        update(-100, "group", title="Team"),  # groups are not what we want
        {"update_id": 9, "callback_query": {"id": "x"}},  # no message at all
        update(77, first_name="Someone Else"),
    ]
    c, seen = client(lambda r: tg(updates))
    chats = await find_chats(c, TOKEN)
    assert [(x.chat_id, x.label) for x in chats] == [(42, "Saif (@saif)"), (77, "Someone Else")]
    assert "getUpdates" in str(seen[0].url)


async def test_find_chats_empty_when_nobody_has_messaged_the_bot() -> None:
    c, _ = client(lambda r: tg([]))
    assert await find_chats(c, TOKEN) == []


async def test_409_conflict_gives_an_actionable_hint() -> None:
    desc = "Conflict: terminated by other getUpdates request"
    c, _ = client(lambda r: httpx.Response(409, json={"ok": False, "description": desc}))
    with pytest.raises(TelegramSetupError, match="already running"):
        await find_chats(c, TOKEN)


async def test_send_test_posts_to_the_chat() -> None:
    c, seen = client(lambda r: tg({"message_id": 5}))
    await send_test(c, TOKEN, 42)
    body = json.loads(seen[0].content)
    assert body["chat_id"] == 42 and "sdr-loop" in body["text"]
    assert str(seen[0].url).endswith("/sendMessage")


async def test_send_test_to_a_chat_the_user_never_started_explains_why() -> None:
    desc = "Forbidden: bot can't initiate conversation with a user"
    c, _ = client(lambda r: httpx.Response(403, json={"ok": False, "description": desc}))
    with pytest.raises(TelegramSetupError, match="Start"):
        await send_test(c, TOKEN, 42)


# ---------------------------------------------------------------- CLI


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch) -> Any:
    def setup(handler: Any, *, token: str = TOKEN, chat_id: str = "") -> list[httpx.Request]:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)
        get_settings.cache_clear()
        c, seen = client(handler)
        monkeypatch.setattr(cli, "_telegram_client", lambda: c)
        return seen

    yield setup
    get_settings.cache_clear()


def routed(updates: list[dict[str, Any]]) -> Any:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/getMe"):
            return tg({"id": 1, "username": "sdr_loop_bot"})
        if req.url.path.endswith("/getUpdates"):
            return tg(updates)
        return tg({"message_id": 1})

    return handler


def test_cli_without_a_token_says_how_to_get_one(cli_env: Any) -> None:
    cli_env(routed([]), token="")
    res = runner.invoke(cli.app, ["telegram"])
    assert res.exit_code == 1 and "BotFather" in res.output


def test_cli_prints_the_chat_id_to_paste_into_env(cli_env: Any) -> None:
    cli_env(routed([update(42, first_name="Saif", username="saif")]))
    res = runner.invoke(cli.app, ["telegram"])
    assert res.exit_code == 0, res.output
    assert "@sdr_loop_bot" in res.output and "TELEGRAM_CHAT_ID=42" in res.output
    assert TOKEN not in res.output


def test_cli_with_no_messages_tells_you_to_press_start(cli_env: Any) -> None:
    cli_env(routed([]))
    res = runner.invoke(cli.app, ["telegram"])
    assert res.exit_code == 1 and "t.me/sdr_loop_bot" in res.output and "Start" in res.output


def test_cli_with_a_chat_id_sends_the_test_message(cli_env: Any) -> None:
    seen = cli_env(routed([]), chat_id="42")
    res = runner.invoke(cli.app, ["telegram"])
    assert res.exit_code == 0, res.output
    assert "Test message sent" in res.output
    assert any(r.url.path.endswith("/sendMessage") for r in seen)
    assert not any(
        r.url.path.endswith("/getUpdates") for r in seen
    )  # already configured: don't poll


def test_cli_reports_a_bad_token_without_echoing_it(cli_env: Any) -> None:
    cli_env(lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
    res = runner.invoke(cli.app, ["telegram"])
    assert res.exit_code == 1 and "Unauthorized" in res.output and TOKEN not in res.output
