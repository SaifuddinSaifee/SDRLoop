from __future__ import annotations

from sdr_loop import kpis
from sdr_loop.memory import Repository
from sdr_loop.models import (
    Claim,
    CodeCheckResult,
    CriticReport,
    CriticScores,
    Email,
    Lead,
    Qualification,
    Sequence,
)  # fmt: skip
from sdr_loop.steps import approve as ap


def seq() -> Sequence:
    e = Email(
        subject="S", body="Body here. Open to a call?", claims=[Claim(text="x", fact_ids=["f1"])]
    )
    return Sequence(email_1=e, email_2=Email(subject="R", body="Bump. Reply?", claims=[]))


def report(f: int = 5) -> dict[str, object]:
    sc = CriticScores(faithfulness=f, personalisation=4, clarity=5, tone=5)
    return CriticReport(code=CodeCheckResult(passed=True), scores=sc).model_dump()


async def add_draft(repo: Repository, run: int, n: int, version: str, f: int = 5) -> int:
    lead = Lead(linkedin_url=f"https://www.linkedin.com/in/k{n}", full_name="K", company="C")
    lid, _ = await repo.upsert_lead(lead, run)
    await repo.save_qualification(lid, Qualification(score=90, verdict="fit"), "q@1")
    return await repo.save_draft(lid, seq(), prompt_version=version, critic=report(f))


async def test_per_prompt_version_kpis_improve_visibly_after_a_promotion(repo: Repository) -> None:
    run = await repo.create_run("u", "v")
    old = [await add_draft(repo, run, i, "writer@local-1") for i in range(4)]
    new = [await add_draft(repo, run, 10 + i, "writer@local-2") for i in range(2)]
    await ap.approve(repo, old[0])
    await ap.edit(
        repo, old[1], "Subject: S\n\nA totally different body for you. Open?\n---\nB. Reply?", []
    )
    await ap.reject(repo, old[2], "too_long")
    await ap.approve(repo, new[0])
    await ap.approve(repo, new[1])
    rows = {r["prompt_version"]: r for r in await kpis.per_prompt_version(repo)}
    v1, v2 = rows["writer@local-1"], rows["writer@local-2"]
    assert (v1["drafts"], v1["reviewed"]) == (4, 3)
    assert v1["approval_rate_no_edits"] == 1 / 3 and v2["approval_rate_no_edits"] == 1.0
    assert v1["avg_edit_distance"] and v1["avg_edit_distance"] > 0  # (0 + n) / 2
    assert v2["avg_edit_distance"] == 0
    assert v1["critic_pass_rate"] == 1.0


async def test_critic_pass_rate_counts_failures_and_unreviewed_drafts_are_excluded(
    repo: Repository,
) -> None:
    run = await repo.create_run("u", "v")
    await add_draft(repo, run, 1, "w@1", f=5)
    await add_draft(repo, run, 2, "w@1", f=2)  # faithfulness 2 -> critic fail
    (row,) = await kpis.per_prompt_version(repo)
    assert row["critic_pass_rate"] == 0.5 and row["reviewed"] == 0
    assert row["approval_rate_no_edits"] is None and row["avg_edit_distance"] is None


async def test_cost_per_qualified_lead_per_run(repo: Repository) -> None:
    run = await repo.create_run("u", "v")
    for i in range(4):
        await add_draft(repo, run, i, "w@1")
    await repo.set_run_status(run, "completed", cost_usd=0.08)
    (r,) = await kpis.per_run(repo)
    assert r["qualified"] == 4 and r["cost_per_qualified"] == 0.02


async def test_empty_database_gives_empty_kpis(repo: Repository) -> None:
    assert await kpis.per_prompt_version(repo) == [] and await kpis.per_run(repo) == []
    assert await kpis.promotions(repo) == []
