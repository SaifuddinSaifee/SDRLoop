from __future__ import annotations

from sdr_loop.memory import Repository
from sdr_loop.models import Lead, Qualification


def lead(url: str = "https://linkedin.com/in/a") -> Lead:
    return Lead(linkedin_url=url, full_name="A B", email="a@x.com", email_status="safe")


async def test_upsert_lead_dedups_by_linkedin_url(repo: Repository) -> None:
    r1 = await repo.create_run("u", "v1")
    r2 = await repo.create_run("u", "v1")
    id1, created1 = await repo.upsert_lead(lead(), r1)
    await repo.set_lead_status(id1, "qualified")
    id2, created2 = await repo.upsert_lead(lead(), r2)
    assert (created1, created2) == (True, False)
    assert id1 == id2
    assert await repo.count_leads() == 1
    row = await repo.get_lead(id1)
    assert row.status == "qualified" and row.run_id == r2  # state kept, linked to new run


async def test_claim_webhook_once(repo: Repository) -> None:
    assert await repo.claim_webhook("ext-1") is True
    assert await repo.claim_webhook("ext-1") is False


async def test_qualification_roundtrip(repo: Repository) -> None:
    run = await repo.create_run("u", "v1")
    lid, _ = await repo.upsert_lead(lead(), run)
    q = Qualification(score=80, verdict="fit", reasons=["a"], decided_by="llm")
    await repo.save_qualification(lid, q, "qualify@1")
    await repo.save_qualification(lid, q.model_copy(update={"score": 90}), "qualify@2")
    got = await repo.get_qualification(lid)
    assert got is not None and got.score == 90 and got.reasons == ["a"]
