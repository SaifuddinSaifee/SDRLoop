"""Setup checks and auto-fixes behind `sdr-loop start` / `sdr-loop check` and the Home page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from sdr_loop import health
from sdr_loop.config import Settings


def client(routes: dict[str, httpx.Response]) -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        for suffix, resp in routes.items():
            if req.url.path.endswith(suffix):
                return resp
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


ME = ok({"username": "sdr_loop_bot"})
UPDATES = ok([{"message": {"chat": {"id": 777, "type": "private", "first_name": "Saif"}}}])


async def test_database_ok_on_sqlite(settings: Settings) -> None:
    c = await health.check_database(settings)
    assert c.status == "ok" and "0 leads" in c.detail


async def test_together_missing_rejected_and_ok(settings: Settings) -> None:
    async with client({}) as http:
        assert (await health.check_together(settings, http)).status == "fail"
    keyed = settings.model_copy(update={"together_api_key": "k"})
    async with client({"/v1/models": httpx.Response(401)}) as http:
        c = await health.check_together(keyed, http)
        assert c.status == "fail" and "rejected" in c.detail
    async with client({"/v1/models": httpx.Response(200, json=[])}) as http:
        assert (await health.check_together(keyed, http)).status == "ok"


async def test_telegram_states(settings: Settings) -> None:
    async with client({}) as http:
        c, _, _ = await health.check_telegram(settings, http)
        assert c.status == "warn" and "web app" in c.detail
    tok = settings.model_copy(update={"telegram_bot_token": "123:abc"})
    async with client({"/getMe": ME, "/getUpdates": ok([])}) as http:
        c, handle, chat = await health.check_telegram(tok, http, discover=True)
        assert (c.status, handle, chat) == ("warn", "@sdr_loop_bot", None)
        assert "https://t.me/sdr_loop_bot" in c.fix
    async with client({"/getMe": ME, "/getUpdates": UPDATES}) as http:
        c, _, chat = await health.check_telegram(tok, http, discover=True)
        assert c.status == "ok" and chat == "777"
    async with client({"/getMe": httpx.Response(401, json={"ok": False})}) as http:
        c, _, _ = await health.check_telegram(tok, http)
        assert c.status == "fail" and "123:abc" not in c.detail  # token never echoed


class FakeLangfuse:
    def __init__(self, auth: bool = True, have: tuple[str, ...] = ()) -> None:
        self.auth, self.have = auth, have

    def auth_check(self) -> bool:
        return self.auth

    def get_prompt(self, name: str, **_: Any) -> object:
        if name not in self.have:
            raise LookupError(name)
        return object()


def test_langfuse_states(settings: Settings) -> None:
    assert health.check_langfuse(settings).status == "warn"
    keyed = settings.model_copy(
        update={"langfuse_public_key": "pk", "langfuse_secret_key": "sk",
                "langfuse_host": "https://jp.cloud.langfuse.com"}
    )  # fmt: skip
    bad = health.check_langfuse(keyed, lambda: FakeLangfuse(auth=False))
    assert bad.status == "fail" and "jp.cloud.langfuse.com" in bad.detail and "region" in bad.fix
    part = health.check_langfuse(keyed, lambda: FakeLangfuse(have=("qualify", "critic")))
    assert part.status == "warn" and "research, writer" in part.detail
    full = ("qualify", "research", "writer", "critic")
    assert health.check_langfuse(keyed, lambda: FakeLangfuse(have=full)).status == "ok"


async def test_prepare_saves_discovered_chat_id(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=123:abc\nTELEGRAM_CHAT_ID=\n")
    tok = settings.model_copy(update={"telegram_bot_token": "123:abc"})
    async with client({"/getMe": ME, "/getUpdates": UPDATES}) as http:
        prep = await health.prepare(tok, env_path=env, client=http)
    assert "TELEGRAM_CHAT_ID=777" in env.read_text()
    assert any("777" in a for a in prep.actions)
    assert prep.get("telegram").status == "ok" and prep.bot_handle == "@sdr_loop_bot"


async def test_prepare_uploads_only_missing_prompts(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sdr_loop import prompts

    keyed = settings.model_copy(update={"langfuse_public_key": "pk", "langfuse_secret_key": "sk"})
    warn = health.Check(
        "langfuse", "Langfuse", "warn", "to upload: writer", action="upload_prompts"
    )
    monkeypatch.setattr(health, "check_langfuse", lambda s: warn)
    monkeypatch.setattr(prompts, "prompts_to_upload", lambda: ["writer"])
    pushed: list[list[str]] = []
    monkeypatch.setattr(
        prompts, "push_local_prompts", lambda names: pushed.append(names) or ["writer@local-1"]
    )
    async with client({}) as http:
        prep = await health.prepare(keyed, env_path=tmp_path / ".env", client=http)
    assert pushed == [["writer"]]
    assert prep.get("langfuse").status == "ok"
    assert any("writer@local-1" in a for a in prep.actions)
