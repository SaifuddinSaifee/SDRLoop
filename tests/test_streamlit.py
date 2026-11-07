"""Drive the Streamlit app headlessly with AppTest (skipped without the ui extra)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from sdr_loop import llm  # noqa: E402
from sdr_loop.cli import app as cli  # noqa: E402
from sdr_loop.config import get_settings  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "app/streamlit_app.py")


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "ui.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("OUTBOX_PATH", str(tmp_path / "outbox.jsonl"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    get_settings.cache_clear()
    res = CliRunner().invoke(cli, ["run", "--fixture", "--fake-llm", "--limit", "10"])
    assert res.exit_code == 0, res.output
    llm.set_gateway(None)
    get_settings.cache_clear()
    return db


def at() -> AppTest:
    t = AppTest.from_file(APP, default_timeout=30)
    t.run()
    assert not t.exception, t.exception
    return t


def test_review_page_shows_draft_next_to_cited_sources(seeded: Path) -> None:
    t = at()
    text = " ".join(m.value for m in t.markdown)
    assert "Cited sources" in " ".join(s.value for s in t.subheader)
    assert "✅ cited" in text and ".example/" in text  # cited fact with its source url
    assert t.text_area[0].value.startswith("Subject:") and "---" in t.text_area[0].value


def test_approve_button_writes_decision_and_outbox(seeded: Path, tmp_path: Path) -> None:
    t = at()
    next(b for b in t.button if "Approve" in b.label).click().run()
    assert not t.exception, t.exception
    c = sqlite3.connect(seeded)
    assert c.execute("select action from decisions").fetchall() == [("approve",)]
    lines = (tmp_path / "outbox.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["dry_run"] is True


def test_edit_and_reject_paths(seeded: Path) -> None:
    t = at()
    t.text_area[0].set_value("Subject: Mine\n\nShort. Open to a call?\n---\nBump. Reply?").run()
    next(b for b in t.button if "edit" in b.label).click().run()
    assert not t.exception, t.exception
    t = at()
    next(b for b in t.button if "Reject" in b.label).click().run()
    c = sqlite3.connect(seeded)
    assert [r[0] for r in c.execute("select action from decisions order by id")] == [
        "edit",
        "reject",
    ]


def test_kpi_page_renders_per_prompt_and_per_run_tables(seeded: Path) -> None:
    t = at()
    t.sidebar.radio[0].set_value("KPIs").run()
    assert not t.exception, t.exception
    assert [h.value for h in t.header] == ["KPIs"]
    assert len(t.dataframe) >= 2
