from __future__ import annotations

import json
from pathlib import Path

import pytest

from sdr_loop.memory import Repository
from sdr_loop.models import Claim, Email, Fact, Lead, Sequence
from sdr_loop.steps import approve as ap
from sdr_loop.steps.outbox import NotApproved, write_outbox

FACTS = [
    Fact(id="f1", source_url="https://a.example/", snippet="Atlas Growth was founded in 2022.")
]


def seq() -> Sequence:
    e1 = Email(
        subject="Quick idea",
        body="Hi A, Atlas Growth started in 2022. Open to a quick call?",
        claims=[Claim(text="started in 2022", fact_ids=["f1"])],
    )
    e2 = Email(subject="Re: Quick idea", body="One more angle. Worth a reply?", claims=[])
    return Sequence(email_1=e1, email_2=e2)


@pytest.fixture
async def draft(repo: Repository) -> tuple[int, int]:
    run = await repo.create_run("u", "v1")
    lead = Lead(
        linkedin_url="https://www.linkedin.com/in/a-b",
        full_name="A B",
        title="Founder",
        company="Atlas Growth",
        email="a@atlas.example",
        email_status="safe",
    )
    lead_id, _ = await repo.upsert_lead(lead, run)
    await repo.save_facts(lead_id, FACTS)
    did = await repo.save_draft(lead_id, seq(), prompt_version="writer@local-1")
    return lead_id, did


def read(path: Path) -> list[dict[str, object]]:
    return [json.loads(x) for x in path.read_text().splitlines()]


async def test_t14_approve_stores_decision_and_sequence_lands_in_outbox(
    repo: Repository, draft: tuple[int, int], tmp_path: Path
) -> None:
    _, did = draft
    out = tmp_path / "outbox.jsonl"
    await ap.approve(repo, did)
    assert await write_outbox(repo, did, out) is True
    rec = read(out)[0]
    assert rec["dry_run"] is True and rec["decision"] == "approve"
    assert rec["email_1"]["subject"] == "Quick idea"  # type: ignore[index]
    d = await repo.get_decision(did)
    assert d is not None and d.edit_distance == 0
    assert (await repo.get_draft_by_id(did)).status == "outboxed"


async def test_outbox_is_idempotent(
    repo: Repository, draft: tuple[int, int], tmp_path: Path
) -> None:
    _, did = draft
    out = tmp_path / "outbox.jsonl"
    await ap.approve(repo, did)
    await write_outbox(repo, did, out)
    assert await write_outbox(repo, did, out) is False
    assert len(read(out)) == 1


async def test_t16_draft_without_approval_never_reaches_outbox(
    repo: Repository, draft: tuple[int, int], tmp_path: Path
) -> None:
    _, did = draft
    out = tmp_path / "outbox.jsonl"
    with pytest.raises(NotApproved):
        await write_outbox(repo, did, out)
    assert not out.exists()
    await ap.reject(repo, did, "bad_tone")  # a rejection is not an approval either
    with pytest.raises(NotApproved):
        await write_outbox(repo, did, out)
    assert not out.exists()


async def test_edit_stores_final_text_distance_and_outbox_uses_the_edit(
    repo: Repository, draft: tuple[int, int], tmp_path: Path
) -> None:
    _, did = draft
    new = (
        "Subject: A better subject\n\nHi A, short and sweet. Open to a call?"
        "\n---\nFollow up here. Reply?"
    )
    await ap.edit(repo, did, new, FACTS)
    d = await repo.get_decision(did)
    assert d is not None and d.action == "edit" and d.edit_distance and d.edit_distance > 10
    out = tmp_path / "o.jsonl"
    await write_outbox(repo, did, out)
    rec = read(out)[0]
    assert rec["email_1"]["subject"] == "A better subject"  # type: ignore[index]
    assert rec["email_2"]["subject"] == "Re: Quick idea"  # type: ignore[index]  # kept from draft


async def test_bad_edit_format_is_rejected_without_recording(
    repo: Repository, draft: tuple[int, int]
) -> None:
    _, did = draft
    with pytest.raises(ap.EditFormatError):
        await ap.edit(repo, did, "only one email, no separator", FACTS)
    assert await repo.get_decision(did) is None


async def test_reject_requires_a_known_reason(repo: Repository, draft: tuple[int, int]) -> None:
    _, did = draft
    with pytest.raises(ValueError):
        await ap.reject(repo, did, "because")
    await ap.reject(repo, did, "wrong_fact")
    d = await repo.get_decision(did)
    assert d is not None and d.reason == "wrong_fact" and d.action == "reject"
    assert (await repo.get_draft_by_id(did)).status == "rejected"


async def test_e14_human_added_claims_are_recorded_not_blocked(
    repo: Repository, draft: tuple[int, int]
) -> None:
    _, did = draft
    text = (
        "Subject: Idea\n\nHi A, congrats on raising $12M with Sequoia. Open to a call?"
        "\n---\nFollow up. Reply?"
    )
    await ap.edit(repo, did, text, FACTS)
    d = await repo.get_decision(did)
    assert d is not None and d.uncited_human_claims is True


async def test_e14_plain_rewording_is_not_marked(repo: Repository, draft: tuple[int, int]) -> None:
    _, did = draft
    text = (
        "Subject: Idea\n\nHi A, Atlas Growth started in 2022. Open to a call?"
        "\n---\nFollow up. Reply?"
    )
    await ap.edit(repo, did, text, FACTS)
    d = await repo.get_decision(did)
    assert d is not None and d.uncited_human_claims is False


def test_review_text_shows_flags_scores_and_only_cited_sources() -> None:
    class D:
        id = 7
        flagged = True
        flag_reason = "non_english_market"
        critic_scores = {
            "scores": {"faithfulness": 5, "personalisation": 4, "clarity": 5, "tone": 5}
        }
        email_1 = seq().email_1.model_dump()
        email_2 = seq().email_2.model_dump()

    lead = Lead(linkedin_url="u", full_name="A B", title="Founder", company="Atlas")
    extra = [*FACTS, Fact(id="f2", source_url="u", snippet="Uncited fact here")]
    t = ap.review_text(lead, D(), extra)
    assert "Draft #7" in t and "non_english_market" in t and "faith 5" in t
    assert "[f1] Atlas Growth was founded" in t and "f2" not in t
