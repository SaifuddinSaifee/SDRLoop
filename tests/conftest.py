from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from sdr_loop import budget
from sdr_loop.config import Settings
from sdr_loop.llm import FakeProvider, LLMGateway, set_gateway
from sdr_loop.memory import Repository
from sdr_loop.observability import InMemoryTracer, set_tracer


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        outbox_path=str(tmp_path / "outbox.jsonl"),
        offline=True,
    )


@pytest.fixture
def tracer() -> Iterator[InMemoryTracer]:
    t = InMemoryTracer()
    set_tracer(t)
    yield t


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
    await r.init_schema()
    yield r
    await r.close()


@pytest.fixture
def fake_llm(settings: Settings, tracer: InMemoryTracer) -> Iterator[dict[str, FakeProvider]]:
    """Gateway wired to one FakeProvider per family. Tests set `.script` on each."""
    fakes = {"anthropic": FakeProvider(), "together": FakeProvider()}
    set_gateway(LLMGateway(dict(fakes), settings, backoff_base=0))  # type: ignore[arg-type]
    yield fakes
    set_gateway(None)
