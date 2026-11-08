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
    assert any("Evaboot API key" in e.value for e in t.error)
    assert JobStore(tmp_path / "state").all() == []


def test_learning_page_renders(seeded: Path) -> None:
    t = at("Learning")
    assert [h.value for h in t.header] == ["Learning"]
    assert any("learning cycle" in b.label for b in t.button)


# ---------------------------------------------------------------------- Setup wizard


@pytest.fixture
def wizard(seeded: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Setup page writing to throwaway copies of .env, campaign.yaml and icp.yaml."""
    import shutil

    from sdr_loop import envfile
    from sdr_loop.ui import setup_wizard

    env = tmp_path / ".env"
    env.write_text("# test\n")
    monkeypatch.setattr(envfile, "ENV_PATH", env)
    shutil.copy("campaign.yaml", tmp_path / "campaign.yaml")
    shutil.copy("icp.yaml", tmp_path / "icp.yaml")
    monkeypatch.setenv("CAMPAIGN_PATH", str(tmp_path / "campaign.yaml"))
    monkeypatch.setattr(setup_wizard, "ICP_PATH", tmp_path / "icp.yaml")
    get_settings.cache_clear()
    return tmp_path


def test_setup_is_the_landing_page_until_required_steps_pass(wizard: Path) -> None:
    t = AppTest.from_file(APP, default_timeout=30)
    t.run()
    assert not t.exception, t.exception
    assert [h.value for h in t.header] == ["Setup"]  # no TOGETHER_API_KEY yet
    labels = [e.label for e in t.expander]
    assert any("1. Company & offer" in x for x in labels)
    assert any("Fresh start" in x for x in labels)


def form_button(t: AppTest, label: str):  # type: ignore[no-untyped-def]
    return next(b for b in t.button if b.label == label)


def test_saving_company_and_offer_writes_campaign_yaml(wizard: Path) -> None:
    import yaml

    t = at("Setup")
    next(x for x in t.text_input if x.label == "Company").set_value("Gather")
    next(x for x in t.text_input if x.label == "Sender name").set_value("Saif S")
    form_button(t, "Save company & offer").click().run()
    assert not t.exception, t.exception
    data = yaml.safe_load((wizard / "campaign.yaml").read_text())
    assert data["company"] == "Gather" and data["sender"]["name"] == "Saif S"
    assert data["allowed_claims"]  # untouched fields kept


def test_keys_are_saved_but_never_shown(wizard: Path) -> None:
    t = at("Setup")
    next(x for x in t.text_input if x.label == "Together AI API key").set_value("tok-SECRET-123")
    form_button(t, "Save and test").click().run()
    assert "TOGETHER_API_KEY=tok-SECRET-123" in (wizard / ".env").read_text()
    t = at("Setup")
    shown = " ".join(str(x.value) for x in t.text_input) + " ".join(m.value for m in t.markdown)
    assert "tok-SECRET-123" not in shown


def test_sales_navigator_urls_are_validated_and_saved(wizard: Path) -> None:
    import yaml

    t = at("Setup")
    box = next(x for x in t.text_area if x.label.startswith("Sales Navigator search URLs"))
    box.set_value("https://evil.example/x")
    next(b for b in t.button if b.label == "Save" and b.key and "leads" in b.key).click().run()
    assert any("Not a Sales Navigator URL" in e.value for e in t.error)
    url = "https://www.linkedin.com/sales/search/people?query=(x)"
    t = at("Setup")
    next(x for x in t.text_area if x.label.startswith("Sales Navigator search URLs")).set_value(url)
    next(b for b in t.button if b.label == "Save" and b.key and "leads" in b.key).click().run()
    assert yaml.safe_load((wizard / "campaign.yaml").read_text())["search_urls"] == [url]


def test_email_delivery_toggle_saves_mode(wizard: Path) -> None:
    t = at("Setup")
    t.toggle[0].set_value(True)
    next(b for b in t.button if b.label == "Save" and b.key and "email" in b.key).click().run()
    env = (wizard / ".env").read_text()
    assert "EMAIL_MODE=mailpit" in env and "FOLLOWUP_DELAY_MINUTES=3" in env


def test_fresh_start_needs_confirmation_then_clears(wizard: Path, seeded: Path) -> None:
    t = at("Setup")
    clear = next(b for b in t.button if b.label == "Clear")
    assert clear.disabled
    next(x for x in t.text_input if x.label == "Type RESET to confirm").set_value("RESET").run()
    next(b for b in t.button if b.label == "Clear").click().run()
    assert not t.exception, t.exception
    assert any("Deleted" in s.value for s in t.success)
    c = sqlite3.connect(seeded)
    assert c.execute("select count(*) from leads").fetchone()[0] == 0
    assert c.execute("select count(*) from drafts").fetchone()[0] == 0
