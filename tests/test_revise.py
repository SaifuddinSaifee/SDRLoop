"""Edit by instruction: 'make it shorter' rewrites the draft (writer + critic), the human approves
the result, and the learning loop still sees the writer's ORIGINAL draft next to the final text."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sdr_loop.bot.controller import BotController
from sdr_loop.config import Settings
from sdr_loop.demo import judge_script, writer_script
from sdr_loop.learn.build_eval_set import build_eval_cases, unused_cases
from sdr_loop.llm import FakeProvider
from sdr_loop.memory import Repository
from sdr_loop.models import Claim, Email, Fact, Lead, Sequence
from sdr_loop.observability import InMemoryTracer
from sdr_loop.steps import revise as rv
from sdr_loop.steps.write import writer_input

OWNER = 42
SHORT = {
    "email_1": {"subject": "Atlas, 2022", "body": "Atlas started in 2022. Open to a call?",
                "claims": [{"text": "started in 2022", "fact_ids": ["f1"]}]},
    "email_2": {"subject": "Re: Atlas, 2022", "body": "One more angle. Worth a reply?",
                "claims": []},
}  # fmt: skip


@pytest.fixture
async def env(
    repo: Repository, settings: Settings, tracer: InMemoryTracer,
    fake_llm: dict[str, FakeProvider],
) -> tuple[BotController, int]:  # fmt: skip
    fake_llm["judge"].script = judge_script
    seen: list[dict[str, object]] = []

    def writer(model, system, messages, tools):  # type: ignore[no-untyped-def]
        seen.append(json.loads(messages[-1]["content"]))
        return json.dumps(SHORT)

    fake_llm["writer"].script = writer
    s = settings.model_copy(update={"telegram_chat_id": str(OWNER)})
    run = await repo.create_run("u", "v1")
    lead = Lead(linkedin_url="https://www.linkedin.com/in/a", full_name="Ana Ruiz",
                title="Founder", company="Atlas", email="ana@atlas.example",
                email_status="safe")  # fmt: skip
    lid, _ = await repo.upsert_lead(lead, run)
    await repo.save_facts(lid, [Fact(id="f1", source_url="https://atlas.example/",
                                     snippet="Atlas was founded in 2022.")])  # fmt: skip
    long = Sequence(
        email_1=Email(subject="A long idea", body="Atlas started in 2022. " + "Words. " * 40
                      + "Open to a call?", claims=[Claim(text="started in 2022", fact_ids=["f1"])]),
        email_2=Email(subject="Re: A long idea", body="Another angle. Worth a reply?", claims=[]),
    )  # fmt: skip
    did = await repo.save_draft(lid, long, prompt_version="writer@local-3")
    c = BotController(repo, s)
    c.seen = seen  # type: ignore[attr-defined]
    return c, did


async def test_instruction_rewrites_the_draft_and_offers_it_again(
    env: tuple[BotController, int],
) -> None:
    c, did = env
    prompt = await c.on_action(OWNER, f"e:{did}")
    assert prompt is not None and "what to change" in prompt.text and prompt.force_reply
    progress: list[str] = []

    async def notify(text: str) -> None:
        progress.append(text)

    reply = await c.on_text(OWNER, "shorter please, keep the 2022 line", notify=notify)
    assert progress and "Rewriting" in progress[0]
    assert reply is not None and "Atlas started in 2022. Open to a call?" in reply.text
    assert "revised" in reply.text and [b[1] for b in reply.buttons[0]] == [
        f"a:{did}", f"e:{did}", f"r:{did}"]  # fmt: skip
    sent = c.seen[-1]  # type: ignore[attr-defined]
    assert "shorter please" in str(sent["revision_notes"])
    assert "Words." in sent["previous_draft"]["email_1"]["body"]  # writer edits, not restarts
    d = await c.repo.get_draft_by_id(did)
    assert d.email_1["subject"] == "Atlas, 2022" and d.status == "pending_approval"
    [r] = await c.repo.list_revisions(did)
    assert r.instruction == "shorter please, keep the 2022 line"
    assert await c.repo.get_decision(did) is None  # nothing decided yet: the human looks again


async def test_full_text_with_separator_is_still_a_manual_edit(
    env: tuple[BotController, int],
) -> None:
    c, did = env
    await c.on_action(OWNER, f"e:{did}")
    reply = await c.on_text(OWNER, "Subject: Mine\n\nShort. Call?\n---\nBump. Reply?")
    assert reply is not None and "saved with your edit" in reply.text
    d = await c.repo.get_decision(did)
    assert d is not None and d.action == "edit"
    assert await c.repo.list_revisions(did) == []


async def test_approving_a_revised_draft_counts_as_an_edit_for_learning(
    env: tuple[BotController, int],
) -> None:
    c, did = env
    original_body = (await c.repo.get_draft_by_id(did)).email_1["body"]
    await c.on_action(OWNER, f"e:{did}")
    await c.on_text(OWNER, "shorter")
    await c.on_action(OWNER, f"a:{did}")
    d = await c.repo.get_decision(did)
    assert d is not None and d.action == "edit" and (d.edit_distance or 0) > 0
    assert "Atlas started in 2022. Open to a call?" in (d.final_text or "")
    out = _outbox(c.settings.outbox_path)
    assert out[0]["email_1"]["body"] == "Atlas started in 2022. Open to a call?"
    assert await build_eval_cases(c.repo) == 1
    [case] = await unused_cases(c.repo)
    assert case.input["original"]["email_1"]["body"] == original_body  # the writer's first try
    assert case.input["revisions"] == ["shorter"]
    assert case.expected is not None and "Open to a call?" in case.expected["final_text"]


def _outbox(path: str) -> list[dict[str, Any]]:
    return [json.loads(x) for x in Path(path).read_text().splitlines()]


async def test_revisions_are_capped(env: tuple[BotController, int]) -> None:
    c, did = env
    for _ in range(rv.MAX_REVISIONS):
        await c.on_action(OWNER, f"e:{did}")
        await c.on_text(OWNER, "again")
    await c.on_action(OWNER, f"e:{did}")
    reply = await c.on_text(OWNER, "once more")
    assert reply is not None and "rewritten" in reply.text and "full text" in reply.text
    assert len(await c.repo.list_revisions(did)) == rv.MAX_REVISIONS


async def test_stranger_cannot_revise(env: tuple[BotController, int]) -> None:
    c, did = env
    await c.on_action(OWNER, f"e:{did}")
    assert await c.on_text(666, "shorter") is None
    assert await c.repo.list_revisions(did) == []


def test_writer_input_carries_previous_draft_only_when_revising() -> None:
    lead = Lead(linkedin_url="u", full_name="A", title="CEO", company="Atlas")
    assert "previous_draft" not in json.loads(writer_input(lead, []))
    prev = Sequence(email_1=Email(subject="s", body="b", claims=[]),
                    email_2=Email(subject="s2", body="b2", claims=[]))  # fmt: skip
    payload = json.loads(writer_input(lead, [], "make it warmer", previous=prev))
    assert payload["previous_draft"]["email_1"] == {"subject": "s", "body": "b"}
    assert "make it warmer" in payload["revision_notes"]


def test_demo_writer_honours_a_revision() -> None:
    """--fake-llm demos show revisions too."""
    payload = {"ABOUT_US": {}, "lead": {"company": "Atlas"}, "facts": [],
               "previous_draft": SHORT, "revision_notes": "shorter"}  # fmt: skip
    msgs = [{"role": "user", "content": json.dumps(payload)}]
    out = json.loads(str(writer_script("m", "You write a two-email x", msgs, None)))  # type: ignore[arg-type]
    assert out["email_1"]["body"] != json.loads(str(writer_script(
        "m", "You write a two-email x",
        [{"role": "user", "content": json.dumps({**payload, "previous_draft": None})}], None,  # type: ignore[list-item]
    )))["email_1"]["body"]  # fmt: skip
    assert judge_script  # imported for the fixture
