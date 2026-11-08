"""Simulated Evaboot: same API as the real client expects, so the rest of the pipeline is real."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from sdr_loop import evaboot_sim, health, llm
from sdr_loop.cli import app
from sdr_loop.config import Settings, get_settings
from sdr_loop.evaboot import BASE_URL, EvabootClient

URL = "https://www.linkedin.com/sales/search/people?query=(filters%3AList())"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


async def test_simulator_speaks_the_documented_protocol() -> None:
    clock = Clock()
    sim = evaboot_sim.EvabootSimulator(extract_seconds=20, clock=clock)
    async with httpx.AsyncClient(transport=sim.transport(), base_url=BASE_URL) as http:
        c = EvabootClient("k", client=http)
        eid = await c.start_extraction(URL, "demo")
        running = await c.get_page(eid)
        assert running["_http_status"] == 202 and running["status"] == "RUNNING"
        clock.t = 10
        assert (await c.get_page(eid))["progress"] == 50
        clock.t = 21
        rows = await c.fetch_all(eid, poll_s=0)
        assert len(rows) == len(sim.prospects()) == 26
        assert {"Full Name", "Email", "Email Status", "Company Website"} <= set(rows[0])
        page = await c.get_page(eid, start=0, limit=10)
        assert len(page["prospects"]) == 10 and page["has_more"] is True
        bad = await http.post("/v1/extractions/url/", json={"linkedin_url": "https://x.com"})
        assert bad.status_code == 400


def test_demo_companies_are_real_sites_and_people_have_evaboot_columns() -> None:
    rows = evaboot_sim.EvabootSimulator().prospects()
    assert all(r["Company Website"].startswith("https://") for r in rows)
    assert not any(r["Company Website"].endswith(".example") for r in rows)
    statuses = {r["Email Status"] for r in rows}
    assert {"safe", "riskier", "invalid", "suspect", ""} <= statuses  # realistic funnel


def test_live_url_run_through_the_simulator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "sim.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")
    monkeypatch.setenv("OUTBOX_PATH", str(tmp_path / "outbox.jsonl"))
    monkeypatch.setenv("EVABOOT_MODE", "simulated")
    monkeypatch.setattr(evaboot_sim, "EXTRACT_SECONDS", 0.0)
    get_settings.cache_clear()
    try:
        res = CliRunner().invoke(app, ["run", URL, "--fake-llm", "--limit", "30"])
    finally:
        llm.set_gateway(None)
    assert res.exit_code == 0, res.output
    assert "Evaboot: simulated" in res.output
    c = sqlite3.connect(db)
    assert c.execute("select search_url from runs").fetchone()[0] == URL
    reasons = dict(c.execute("select skip_reason, count(*) from leads group by skip_reason"))
    assert reasons.get("no_email") == 1  # skipped at extract, as with a real export
    assert c.execute("select count(*) from drafts").fetchone()[0] >= 3


async def test_evaboot_check_states(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from sdr_loop import campaign

    def http(resp: httpx.Response) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda r: resp))

    sim = settings.model_copy(update={"evaboot_mode": "simulated"})
    live = settings.model_copy(update={"evaboot_api_key": "k"})
    no_seat = httpx.Response(200, json={"quota": {"credits": 0.0, "has_valid_salesnav": False}})
    async with http(no_seat) as h:
        c = await health.check_evaboot(live, h)
        assert c.status == "warn" and "no Sales Navigator seat" in c.detail and "Simulated" in c.fix
    async with http(httpx.Response(401)) as h:
        assert (await health.check_evaboot(live, h)).status == "fail"
    real = campaign.load_campaign
    monkeypatch.setattr(campaign, "load_campaign", lambda p=None: (
        real(p)[0].model_copy(update={"search_urls": [URL]}), "v"))  # fmt: skip
    monkeypatch.setattr(health, "load_campaign", campaign.load_campaign)
    async with http(httpx.Response(500)) as h:
        ok = await health.check_evaboot(sim, h)
        assert ok.status == "ok" and "Simulated" in ok.detail
