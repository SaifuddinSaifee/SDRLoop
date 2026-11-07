"""Smoke test through the real CLI entry point (offline fixtures, canned models)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdr_loop import llm
from sdr_loop.cli import app
from sdr_loop.config import get_settings

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "cli.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("OUTBOX_PATH", str(tmp_path / "outbox.jsonl"))
    get_settings.cache_clear()
    yield db  # type: ignore[misc]
    get_settings.cache_clear()
    llm.set_gateway(None)


def test_fixture_fake_llm_run_end_to_end(cli_env: Path) -> None:
    res = runner.invoke(app, ["run", "--fixture", "--fake-llm", "--limit", "20"])
    assert res.exit_code == 0, res.output
    assert "Run 1: completed" in res.output and "await approval" in res.output
    c = sqlite3.connect(cli_env)
    n_drafts = c.execute("select count(*) from drafts").fetchone()[0]
    assert n_drafts > 0
    # research really fetched the offline pages (regression: DNS check must be bypassed offline)
    assert c.execute("select count(*) from facts").fetchone()[0] >= n_drafts
    no_facts = c.execute(
        "select count(*) from drafts where flag_reason like '%no_facts%'"
    ).fetchone()
    assert no_facts[0] == 0
    # nothing is ever sent without approval
    assert c.execute("select count(*) from decisions").fetchone()[0] == 0


def test_rerun_resume_does_not_duplicate(cli_env: Path) -> None:
    runner.invoke(app, ["run", "--fixture", "--fake-llm", "--limit", "6"])
    res = runner.invoke(app, ["run", "--resume", "1", "--fake-llm", "--limit", "6"])
    assert res.exit_code == 0, res.output
    c = sqlite3.connect(cli_env)
    assert (
        c.execute("select count(*) from drafts").fetchone()[0]
        == c.execute("select count(distinct lead_id) from drafts").fetchone()[0]
    )


def test_refuses_to_start_without_models_or_with_dry_run_off(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = runner.invoke(app, ["run", "--fixture"])
    assert res.exit_code == 1 and "ANTHROPIC_API_KEY" in res.output
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    res = runner.invoke(app, ["run", "--fixture", "--fake-llm"])
    assert res.exit_code == 1 and "no sending adapters" in res.output


def test_run_requires_an_input() -> None:
    res = runner.invoke(app, ["run", "--fake-llm"])
    assert res.exit_code == 1


def test_http_client_loggers_are_silenced_so_tokens_never_reach_logs() -> None:
    import logging

    from sdr_loop.cli import _setup_logging

    _setup_logging()
    for name in ("httpx", "httpcore", "telegram"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
