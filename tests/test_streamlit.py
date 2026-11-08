"""Drive the control panel headlessly with AppTest."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from typer.testing import CliRunner

from sdr_loop import llm
from sdr_loop.cli import app as cli
from sdr_loop.config import get_settings
from sdr_loop.jobs import Job, JobStore

APP = str(Path(__file__).resolve().parents[1] / "src/sdr_loop/ui/streamlit_app.py")


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "ui.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("OUTBOX_PATH", str(tmp_path / "outbox.jsonl"))
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    get_settings.cache_clear()
    res = CliRunner().invoke(cli, ["run", "--fixture", "--fake-llm", "--limit", "10"])
    assert res.exit_code == 0, res.output
    llm.set_gateway(None)
    get_settings.cache_clear()
    return db


def at(page: str = "Review queue") -> AppTest:
    t = AppTest.from_file(APP, default_timeout=30)
    t.run()
    assert not t.exception, t.exception
    if page != "Home":
        t.sidebar.radio[0].set_value(page).run()
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
    t = at("KPIs")
    assert [h.value for h in t.header] == ["KPIs"]
    assert len(t.dataframe) >= 2


def test_home_shows_counters_setup_and_run_form(seeded: Path) -> None:
    t = at("Home")
    metrics = {m.label: m.value for m in t.metric}
    assert int(metrics["Drafts waiting for you"]) >= 1
    names = " ".join(m.value for m in t.markdown)
    assert "Together AI" in names and "Telegram" in names and "Langfuse" in names
    assert t.checkbox[0].value is True  # no TOGETHER_API_KEY: demo mode is pre-ticked


def test_start_run_button_launches_a_background_job(seeded: Path, tmp_path: Path) -> None:
    t = at("Home")
    t.number_input[0].set_value(2)
    next(b for b in t.button if "Start run" in b.label).click().run()
    assert not t.exception, t.exception
    [job] = JobStore(tmp_path / "state").all()
    assert job.argv[-4:] == ["--fixture", "--limit", "2", "--fake-llm"] and job.source == "web"
    import time

    for _ in range(600):
        if Job.load(job.path).finished_at:
            break
        time.sleep(0.1)
    assert Job.load(job.path).state == "done", Job.load(job.path).log_tail()


def test_live_run_without_evaboot_key_is_refused(seeded: Path, tmp_path: Path) -> None:
    t = at("Home")
    next(r for r in t.radio if r.label == "Leads from").set_value(
        "Live Sales Navigator search (Evaboot)"
    )
    t.text_input[0].set_value("https://www.linkedin.com/sales/search/people?x=1")
    next(b for b in t.button if "Start run" in b.label).click().run()
    assert any("EVABOOT_API_KEY" in e.value for e in t.error)
    assert JobStore(tmp_path / "state").all() == []


def test_learning_page_renders(seeded: Path) -> None:
    t = at("Learning")
    assert [h.value for h in t.header] == ["Learning"]
    assert any("learning cycle" in b.label for b in t.button)
