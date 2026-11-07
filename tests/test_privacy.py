from __future__ import annotations

import json
from pathlib import Path

import httpx
from sqlalchemy import func, select

from sdr_loop.bot.controller import BotController
from sdr_loop.config import Settings
from sdr_loop.db.schema import DecisionRow, DraftRow, EvalCase, FactRow, LeadRow, QualificationRow
from sdr_loop.demo import fake_gateway
from sdr_loop.learn.build_eval_set import build_eval_cases
from sdr_loop.llm import set_gateway
from sdr_loop.memory import Repository
from sdr_loop.observability import InMemoryTracer, mask_pii
from sdr_loop.pipeline import Pipeline
from sdr_loop.privacy import purge_lead
from sdr_loop.steps.extract import ingest
from sdr_loop.steps.qualify import load_icp
from sdr_loop.tools.offline import offline_transport
from tests.test_pipeline import public, records

ICP, ICP_V = load_icp(Path("icp.yaml"))


def read(path: str) -> str:
    return Path(path).read_text()


async def count(repo: Repository, model: type) -> int:
    async with repo.session() as s:
        return int((await s.execute(select(func.count()).select_from(model))).scalar_one())


async def test_t22_purge_deletes_lead_data_outbox_lines_and_trace(
    repo: Repository, settings: Settings, tracer: InMemoryTracer
) -> None:
    s = settings.model_copy(update={"telegram_chat_id": "42"})
    set_gateway(fake_gateway(s))
    recs = records(2)
    run = await repo.create_run("fixture", ICP_V)
    await ingest(repo, run, recs)
    http = httpx.AsyncClient(transport=offline_transport())
    await Pipeline(repo, s, ICP, ICP_V, http, public).run(run)

    # a human edit on lead 1 (becomes an eval case), an approval on lead 2
    bot = BotController(repo, s)
    pushed = await bot.push_pending()
    ids = sorted(d for d, _ in pushed)
    await bot.on_action(42, f"e:{ids[0]}")
    await bot.on_text(42, "Subject: S\n\nShort one. Open to a call?\n---\nBump. Reply?")
    await bot.on_action(42, f"a:{ids[1]}")
    await build_eval_cases(repo)
    # draft ids follow completion order (leads run concurrently): find the victim via its draft
    victim = await repo.get_lead((await repo.get_draft_by_id(ids[0])).lead_id)
    other = await repo.get_lead((await repo.get_draft_by_id(ids[1])).lead_id)

    # before the purge: traces never carried a raw email (masked), but exist
    dump = json.dumps([[sp.input, sp.fields, sp.events] for t in tracer.traces for sp in t.spans],
                      default=str)  # fmt: skip
    assert victim.email not in dump and other.email not in dump
    outbox = read(s.outbox_path)
    assert victim.email in outbox and other.email in outbox
    assert len(tracer.traces) == 2

    rep = await purge_lead(repo, tracer, victim.linkedin_url, s.outbox_path)

    assert rep.found and rep.trace_deleted and rep.outbox_lines_removed == 1
    assert rep.rows["leads"] == 1 and rep.rows["drafts"] == 1 and rep.rows["facts"] >= 1
    assert rep.rows["decisions"] == 1 and rep.rows["eval_cases"] == 1
    async with repo.session() as sess:
        assert await sess.get(LeadRow, victim.id) is None
        assert (
            await sess.execute(select(DraftRow).where(DraftRow.lead_id == victim.id))
        ).first() is None
        assert (
            await sess.execute(select(FactRow).where(FactRow.lead_id == victim.id))
        ).first() is None
        assert await sess.get(QualificationRow, victim.id) is None
    assert await count(repo, EvalCase) == 0  # the case built from the victim's edit is gone too
    assert [t.trace_id for t in tracer.traces] == [other.trace_id]
    left = read(s.outbox_path)
    assert victim.email not in left and other.email in left
    # the other lead is untouched
    assert await repo.get_lead(other.id) is not None
    assert await count(repo, DecisionRow) == 1


async def test_purge_unknown_lead_reports_not_found(repo: Repository, settings: Settings) -> None:
    rep = await purge_lead(
        repo, InMemoryTracer(), "https://www.linkedin.com/in/nobody", settings.outbox_path
    )
    assert rep.found is False


async def test_purge_by_numeric_id_and_url_forms(repo: Repository, settings: Settings) -> None:
    run = await repo.create_run("u", "v")
    await ingest(repo, run, records(1))
    (row,) = await repo.list_leads(run)
    assert (await repo.find_lead(row.id)).id == row.id  # type: ignore[union-attr]
    assert (await repo.find_lead(str(row.id))).id == row.id  # type: ignore[union-attr]
    for variant in (
        row.linkedin_url + "/",
        row.linkedin_url + "?utm_source=x",
        row.linkedin_url.replace("https://www.", "https://"),
    ):
        found = await repo.find_lead(variant)
        assert found is not None and found.id == row.id, variant
    assert await repo.find_lead("https://www.linkedin.com/in/nobody") is None


def test_trace_recorder_masks_emails_and_phones_like_langfuse_does() -> None:
    t = InMemoryTracer()
    with t.trace("lead", seed="s"), t.span("x", input={"to": "a.b@acme.com"}) as sp:
        sp.update(output="call +33 6 12 34 56 78 or mail c@d.io")
        sp.event("note", who="e@f.org")
    span = t.traces[0].spans[0]
    assert span.input == {"to": "[EMAIL]"}
    assert span.fields["output"] == "call [PHONE] or mail [EMAIL]"
    assert span.events[0][1] == {"who": "[EMAIL]"}
    assert mask_pii(["ok", 3]) == ["ok", 3]
