from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from sdr_loop.api.webhooks import create_app
from sdr_loop.config import Settings
from sdr_loop.evaboot import EvabootClient, EvabootError
from sdr_loop.memory import Repository
from sdr_loop.steps.extract import (
    ingest,
    load_csv,
    normalise_linkedin_url,
    record_to_lead,
    skip_reason,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/evaboot_export.csv"


async def test_t1_fixture_parsed_and_unverified_skipped_with_reason(repo: Repository) -> None:
    records = load_csv(FIXTURE)
    run = await repo.create_run("fixture", "v1")
    rep = await ingest(repo, run, records)
    assert rep.total == len(records) == 306
    assert rep.invalid == 0
    assert rep.duplicates == 6  # trailing-slash dups collapse onto the same lead
    assert rep.new == 300
    assert rep.skipped["no_email"] == 20
    assert sum(v for k, v in rep.skipped.items() if k.startswith("email_not_verified")) == 20
    assert rep.processable == 260
    skipped_rows = [r for r in await repo.list_leads(run) if r.status == "skipped"]
    assert len(skipped_rows) == 40 and all(r.skip_reason for r in skipped_rows)


async def test_t3_rerun_creates_no_duplicates(repo: Repository) -> None:
    records = load_csv(FIXTURE)
    r1 = await repo.create_run("fixture", "v1")
    await ingest(repo, r1, records)
    first = await repo.count_leads()
    r2 = await repo.create_run("fixture", "v1")
    rep2 = await ingest(repo, r2, records)
    assert await repo.count_leads() == first == 300
    assert rep2.new == 0


def test_url_normalisation() -> None:
    a = normalise_linkedin_url("https://linkedin.com/in/Jo-Doe/?utm=1")
    b = normalise_linkedin_url("https://www.linkedin.com/in/jo-doe")
    assert a.lower() == b.lower() == "https://www.linkedin.com/in/jo-doe"


def test_column_aliases_and_headcount_ranges() -> None:
    lead = record_to_lead(
        {
            "Name": "A B",
            "Job Title": "Founder",
            "Company": "X",
            "Company Size": "11-50 employees",
            "Profile URL": "https://linkedin.com/in/ab",
            "Email": "A@X.com",
            "Email Status": "Safe",
        }
    )
    assert lead is not None
    assert (lead.full_name, lead.headcount, lead.email, lead.email_status) == (
        "A B",
        50,
        "a@x.com",
        "safe",
    )
    assert skip_reason(lead) is None
    assert record_to_lead({"Full Name": "no url"}) is None


# ---------------------------------------------------------------- webhook (T2, E2)


def payload() -> dict[str, Any]:
    return {
        "extraction_id": "ext-1",
        "status": "EXECUTED",
        "prospects": [
            {
                "Full Name": "A B",
                "Current Job": "Founder",
                "Company Name": "Acme",
                "LinkedIn Profile URL": "https://www.linkedin.com/in/a-b",
                "Email": "a@acme.com",
                "Email Status": "safe",
                "Country": "US",
                "Headcount": "20",
            },
            {
                "Full Name": "C D",
                "LinkedIn Profile URL": "https://www.linkedin.com/in/c-d",
                "Email": "",
                "Email Status": "",
            },
        ],
    }


@pytest.fixture
async def webhook_env(repo: Repository, settings: Settings) -> tuple[AsyncClient, int]:
    run = await repo.create_run("https://linkedin.com/sales/search/people?x=1", "v1", "ext-1")
    app = create_app(repo, settings)
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    return client, run


async def test_t2_same_webhook_twice_stores_one_set(
    repo: Repository, settings: Settings, webhook_env: tuple[AsyncClient, int]
) -> None:
    client, run = webhook_env
    url = f"/webhooks/evaboot/{settings.evaboot_webhook_secret}/{run}"
    r1 = await client.post(url, json=payload())
    r2 = await client.post(url, json=payload())
    assert r1.json()["status"] == "ok" and r2.json()["status"] == "duplicate"
    assert await repo.count_leads() == 2
    assert (await repo.get_run(run)).status == "extracted"  # type: ignore[union-attr]


async def test_webhook_rejects_bad_secret_and_unknown_run(
    settings: Settings, webhook_env: tuple[AsyncClient, int]
) -> None:
    client, run = webhook_env
    assert (await client.post(f"/webhooks/evaboot/wrong/{run}", json=payload())).status_code == 404
    good = settings.evaboot_webhook_secret
    assert (await client.post(f"/webhooks/evaboot/{good}/999", json=payload())).status_code == 404
    bad = payload() | {"extraction_id": "other"}
    assert (await client.post(f"/webhooks/evaboot/{good}/{run}", json=bad)).status_code == 400


async def test_webhook_failed_extraction_marks_run_failed(
    repo: Repository, settings: Settings, webhook_env: tuple[AsyncClient, int]
) -> None:
    client, run = webhook_env
    body = {"extraction_id": "ext-1", "status": "FAILED", "failure_reason": "quota"}
    await client.post(f"/webhooks/evaboot/{settings.evaboot_webhook_secret}/{run}", json=body)
    r = await repo.get_run(run)
    assert r is not None and r.status == "failed" and "quota" in (r.failure_reason or "")


# ---------------------------------------------------------------- API client / polling (E1)


def evaboot_transport(pages: list[httpx.Response]) -> httpx.MockTransport:
    calls = iter(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.headers["authorization"] == "Bearer k"
            body = json.loads(request.content)
            assert body["linkedin_url"].startswith("https://www.linkedin.com/sales/")
            return httpx.Response(202, json={"extraction_id": "ext-9", "status": "ACCEPTED"})
        return next(calls)

    return httpx.MockTransport(handler)


async def test_client_start_and_poll_until_executed_with_paging() -> None:
    pages = [
        httpx.Response(202, json={"status": "EXECUTING", "progress": 40}),
        httpx.Response(200, json={"status": "EXECUTED", "prospects": [{"a": 1}], "has_more": True}),
        httpx.Response(
            200, json={"status": "EXECUTED", "prospects": [{"a": 2}], "has_more": False}
        ),
    ]
    http = httpx.AsyncClient(base_url="https://api.evaboot.com", transport=evaboot_transport(pages))
    c = EvabootClient("k", client=http)
    eid = await c.start_extraction("https://www.linkedin.com/sales/search/people?q=1", "demo")
    assert eid == "ext-9"
    assert await c.fetch_all(eid, poll_s=0) == [{"a": 1}, {"a": 2}]


async def test_client_poll_times_out_with_clear_error() -> None:
    pages = [httpx.Response(202, json={"status": "EXECUTING"})] * 5
    http = httpx.AsyncClient(base_url="https://api.evaboot.com", transport=evaboot_transport(pages))
    with pytest.raises(EvabootError, match="not finished"):
        await EvabootClient("k", client=http).fetch_all("ext-9", poll_s=0, timeout_s=0)


# ---------------------------------------------------------------- live extraction fallback (E1)


def live_client(handler_pages: list[httpx.Response]) -> EvabootClient:
    http = httpx.AsyncClient(
        base_url="https://api.evaboot.com", transport=evaboot_transport(handler_pages)
    )
    return EvabootClient("k", client=http)


async def test_e1_webhook_never_arrives_so_we_poll_and_ingest(repo: Repository) -> None:
    from sdr_loop.steps.extract import run_live_extraction

    run = await repo.create_run("https://www.linkedin.com/sales/search/people?q=1", "v1")
    pages = [httpx.Response(200, json={"status": "EXECUTED", "prospects": payload()["prospects"]})]
    await run_live_extraction(
        repo, live_client(pages), run, "https://www.linkedin.com/sales/search/people?q=1",
        "https://x/hook", webhook_wait_s=0, poll_s=0,
    )  # fmt: skip
    r = await repo.get_run(run)
    assert r is not None and r.status == "extracted" and r.extraction_id == "ext-9"
    assert await repo.count_leads() == 2


async def test_e1_webhook_and_poll_both_fail_marks_run_failed_with_reason(repo: Repository) -> None:
    from sdr_loop.steps.extract import run_live_extraction

    run = await repo.create_run("u", "v1")
    pages = [httpx.Response(202, json={"status": "EXECUTING"})] * 3
    await run_live_extraction(
        repo, live_client(pages), run, "https://www.linkedin.com/sales/search/people?q=1",
        "https://x/hook", webhook_wait_s=0, poll_s=0, poll_timeout_s=0,
    )  # fmt: skip
    r = await repo.get_run(run)
    assert r is not None and r.status == "failed" and "not finished" in (r.failure_reason or "")
    assert await repo.count_leads() == 0


async def test_webhook_arriving_during_the_wait_ends_it_without_polling(
    repo: Repository, settings: Settings
) -> None:
    from sdr_loop.steps.extract import run_live_extraction

    run = await repo.create_run("u", "v1")
    client = live_client([])  # any poll would raise StopIteration: proves we never poll

    async def webhook_lands() -> None:
        await asyncio.sleep(0.01)
        await repo.set_run_status(run, "extracted")

    await asyncio.gather(
        run_live_extraction(repo, client, run, "https://www.linkedin.com/sales/x", "https://x/h",
                            webhook_wait_s=5, poll_s=0.005),
        webhook_lands(),
    )  # fmt: skip
    r = await repo.get_run(run)
    assert r is not None and r.status == "extracted"
