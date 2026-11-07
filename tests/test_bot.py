from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sdr_loop.bot.controller import BotController
from sdr_loop.config import Settings
from sdr_loop.memory import Repository
from sdr_loop.models import Claim, Email, Fact, Lead, Sequence
from sdr_loop.observability import InMemoryTracer, lead_trace_seed

OWNER, STRANGER = 42, 666


@pytest.fixture
async def env(
    repo: Repository, settings: Settings, tracer: InMemoryTracer
) -> tuple[BotController, int, int]:
    s = settings.model_copy(update={"telegram_chat_id": str(OWNER)})
    run = await repo.create_run("u", "v1")
    lead = Lead(
        linkedin_url="https://www.linkedin.com/in/a-b",
        full_name="A B",
        title="Founder",
        company="Atlas",
        email="a@atlas.example",
        email_status="safe",
    )
    lid, _ = await repo.upsert_lead(lead, run)
    await repo.save_facts(
        lid,
        [Fact(id="f1", source_url="https://atlas.example/", snippet="Atlas was founded in 2022.")],
    )
    seq = Sequence(
        email_1=Email(
            subject="Idea",
            body="Atlas started in 2022. Open to a call?",
            claims=[Claim(text="started in 2022", fact_ids=["f1"])],
        ),
        email_2=Email(subject="Re: Idea", body="One more angle. Worth a reply?", claims=[]),
    )
    did = await repo.save_draft(lid, seq, prompt_version="writer@local-1")
    return BotController(repo, s), lid, did


def outbox(c: BotController) -> list[dict[str, object]]:
    p = Path(c.settings.outbox_path)
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


async def test_t15_unknown_chat_is_ignored_and_logged(
    env: tuple[BotController, int, int], caplog: pytest.LogCaptureFixture
) -> None:
    c, _, did = env
    with caplog.at_level(logging.WARNING):
        assert await c.on_action(STRANGER, f"a:{did}") is None
        assert await c.on_text(STRANGER, "hello") is None
    assert "unauthorised chat 666" in caplog.text
    assert await c.repo.get_decision(did) is None and outbox(c) == []


async def test_nobody_is_authorised_when_chat_id_unset(
    env: tuple[BotController, int, int],
) -> None:
    c, _, did = env
    c.settings = c.settings.model_copy(update={"telegram_chat_id": ""})
    assert await c.on_action(0, f"a:{did}") is None and await c.on_action("", f"a:{did}") is None


async def test_t14_approve_button_stores_decision_and_writes_outbox(
    env: tuple[BotController, int, int],
) -> None:
    c, _, did = env
    reply = await c.on_action(OWNER, f"a:{did}")
    assert reply is not None and "approved" in reply.text
    d = await c.repo.get_decision(did)
    assert d is not None and d.action == "approve"
    assert len(outbox(c)) == 1 and outbox(c)[0]["dry_run"] is True


async def test_double_tap_does_not_decide_twice(env: tuple[BotController, int, int]) -> None:
    c, _, did = env
    await c.on_action(OWNER, f"a:{did}")
    again = await c.on_action(OWNER, f"r:{did}")
    assert again is not None and "already" in again.text
    assert len(outbox(c)) == 1


async def test_edit_flow_waits_for_text_then_records_edit(
    env: tuple[BotController, int, int],
) -> None:
    c, _, did = env
    prompt = await c.on_action(OWNER, f"e:{did}")
    assert prompt is not None and prompt.force_reply
    assert (await c.repo.get_decision(did)) is None  # nothing decided yet
    bad = await c.on_text(OWNER, "no separator here")
    assert bad is not None and "Could not parse" in bad.text and bad.force_reply
    ok = await c.on_text(OWNER, "Subject: S\n\nShort one. Open to a call?\n---\nBump. Reply?")
    assert ok is not None and "saved" in ok.text
    d = await c.repo.get_decision(did)
    assert d is not None and d.action == "edit" and (d.edit_distance or 0) > 0
    assert outbox(c)[0]["decision"] == "edit"
    assert (await c.on_text(OWNER, "stray text")) is not None  # no pending edit -> hint only
    assert len(outbox(c)) == 1


async def test_reject_asks_for_reason_then_records_it_without_outbox(
    env: tuple[BotController, int, int],
) -> None:
    c, lid, did = env
    menu = await c.on_action(OWNER, f"r:{did}")
    assert menu is not None
    labels = [b[0] for row in menu.buttons for b in row]
    assert labels == ["wrong fit", "wrong fact", "bad tone", "too long", "other"]
    await c.on_action(OWNER, f"rr:{did}:wrong_fact")
    d = await c.repo.get_decision(did)
    assert d is not None and d.action == "reject" and d.reason == "wrong_fact"
    assert outbox(c) == []
    assert (await c.repo.get_lead(lid)).status == "rejected"


async def test_push_pending_sends_each_draft_once_with_three_buttons(
    env: tuple[BotController, int, int],
) -> None:
    c, _, did = env
    first = await c.push_pending()
    assert [i for i, _ in first] == [did]
    reply = first[0][1]
    assert [b[0] for b in reply.buttons[0]] == ["✅ Approve", "✏️ Edit", "🗑 Reject"]
    assert "[f1] Atlas was founded in 2022." in reply.text
    assert await c.push_pending() == []


async def test_e13_reminder_only_for_drafts_pending_over_a_day(
    env: tuple[BotController, int, int],
) -> None:
    c, _, _ = env
    assert await c.reminder() is None
    later = datetime.now(UTC) + timedelta(hours=25)
    text = await c.reminder(now=later)
    assert text is not None and "1 draft" in text
    assert outbox(c) == []  # a pending draft is never sent


async def test_decision_appends_approve_and_outbox_spans_and_scores_to_the_lead_trace(
    env: tuple[BotController, int, int], tracer: InMemoryTracer
) -> None:
    c, lid, did = env
    await c.on_action(OWNER, f"a:{did}")
    (trace,) = tracer.traces
    assert trace.trace_id == tracer.traces[0].trace_id
    assert [s.name for s in trace.step_spans()] == ["approve", "outbox"]
    assert trace.scores["human_decision"] == "approve" and trace.scores["edit_distance"] == 0.0
    assert lead_trace_seed(lid) == f"lead:{lid}"
