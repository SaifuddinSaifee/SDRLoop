from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from sdr_loop import budget
from sdr_loop.config import Settings, get_settings
from sdr_loop.db.schema import Base
from sdr_loop.demo import routes_for_fakes
from sdr_loop.llm import FakeProvider, LLMGateway, set_gateway
from sdr_loop.memory import Repository
from sdr_loop.observability import InMemoryTracer, set_tracer


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # TEST_DATABASE_URL=postgresql+asyncpg://... runs the whole suite against Postgres.
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=os.environ.get("TEST_DATABASE_URL")
        or f"sqlite+aiosqlite:///{tmp_path}/test.db",
        outbox_path=str(tmp_path / "outbox.jsonl"),
        offline=True,
    )


@pytest.fixture
def tracer() -> Iterator[InMemoryTracer]:
    t = InMemoryTracer()
    set_tracer(t)
    yield t


SECRETS = (
    "TOGETHER_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
    "EVABOOT_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "PUBLIC_WEBHOOK_URL",
)  # fmt: skip


@pytest.fixture(autouse=True)
def _restore_environ() -> Iterator[None]:
    """The Setup page applies saved settings to os.environ; never let that leak between tests."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def _no_real_services(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Env vars beat .env, so a developer's real keys can never leak into unit tests."""
    for k in SECRETS:
        monkeypatch.setenv(k, "")
    monkeypatch.setenv("OFFLINE", "true")
    monkeypatch.setenv("EMAIL_MODE", "off")
    monkeypatch.setenv("EVABOOT_MODE", "live")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_budgets() -> Iterator[None]:
    budget.set_run_budget(None)
    budget.set_lead_budget(None)
    yield
    budget.set_run_budget(None)
    budget.set_lead_budget(None)


@pytest.fixture
async def repo(settings: Settings) -> AsyncIterator[Repository]:
    r = Repository(settings.database_url)
    async with r.engine.begin() as conn:  # clean slate (matters when the DB is shared: Postgres)
        await conn.run_sync(Base.metadata.drop_all)
    await r.init_schema()
    yield r
    await r.close()


@pytest.fixture
def fake_llm(settings: Settings, tracer: InMemoryTracer) -> Iterator[dict[str, FakeProvider]]:
    """Gateway wired to two FakeProviders: "writer" (write/research/challenger) and "judge"
    (qualify/critic), on the real route table's model ids. Tests set `.script` on each."""
    fakes = {"writer": FakeProvider(), "judge": FakeProvider()}
    gateway = LLMGateway(dict(fakes), settings, backoff_base=0, routes=routes_for_fakes())  # type: ignore[arg-type]
    set_gateway(gateway)
    yield fakes
    set_gateway(None)
