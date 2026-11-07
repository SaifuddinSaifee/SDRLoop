from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from evals.golden import golden_leads

from sdr_loop import llm
from sdr_loop.bot.controller import BotController
from sdr_loop.config import Settings
from sdr_loop.demo import judge_script, routes_for_fakes, writer_script
from sdr_loop.llm import FakeProvider, LLMGateway, RawResult
from sdr_loop.memory import Repository
from sdr_loop.observability import InMemoryTracer
from sdr_loop.pipeline import Pipeline
from sdr_loop.steps.extract import ingest
from sdr_loop.steps.qualify import load_icp
from sdr_loop.tools.offline import offline_transport

ICP, ICP_V = load_icp(__import__("pathlib").Path("icp.yaml"))


async def public(_: str) -> bool:
    return True


def records(n_fit: int, n_other: int = 0) -> list[dict[str, Any]]:
    # re-serialise golden leads as Evaboot-style rows
    out: list[dict[str, Any]] = []
    fit = [x for x in golden_leads() if x[1] == "fit"][:n_fit]
    other = [x for x in golden_leads() if x[1] != "fit" and x[0].industry][:n_other]
    for lead, _ in fit + other:
        out.append({
            "Full Name": lead.full_name, "Current Job": lead.title, "Company Name": lead.company,
            "Company Website": lead.company_website, "Industry": lead.industry,
            "Headcount": lead.headcount, "Country": lead.country,
            "LinkedIn Profile URL": lead.linkedin_url, "Email": lead.email, "Email Status": "safe",
        })  # fmt: skip
    return out


def wire(settings: Settings, **overrides: Any) -> tuple[Settings, dict[str, FakeProvider]]:
    s = settings.model_copy(update={"telegram_chat_id": "42", **overrides})
    fakes = {"writer": FakeProvider(writer_script), "judge": FakeProvider(judge_script)}
    llm.set_gateway(LLMGateway(dict(fakes), s, backoff_base=0, routes=routes_for_fakes()))  # type: ignore[arg-type]
    return s, fakes


def pipeline(repo: Repository, s: Settings) -> Pipeline:
    http = httpx.AsyncClient(transport=offline_transport())
    return Pipeline(repo, s, ICP, ICP_V, http, public)


async def new_run(repo: Repository, recs: list[dict[str, Any]]) -> int:
    run = await repo.create_run("fixture", ICP_V)
    await ingest(repo, run, recs)
    return run


@pytest.fixture(autouse=True)
def _cleanup() -> Any:
    yield
    llm.set_gateway(None)


async def test_t21_one_lead_one_trace_seven_step_spans_with_cost_and_all_scores(
    repo: Repository, settings: Settings, tracer: InMemoryTracer
) -> None:
    s, _ = wire(settings)
    run = await new_run(repo, records(1))
    report = await pipeline(repo, s).run(run)
    assert report.counts == {"pending_approval": 1}

    # the human decides later: approve + outbox join the SAME trace
    bot = BotController(repo, s)
    ((_, reply),) = await bot.push_pending()
    draft_id = int(reply.buttons[0][0][1].split(":")[1])
    await bot.on_action(42, f"a:{draft_id}")

    (trace,) = tracer.traces
    steps = trace.step_spans()
    assert [x.name for x in steps] == [
        "extract", "qualify", "research", "write", "critic", "approve", "outbox",
    ]  # fmt: skip
    llm_steps = {"qualify", "research", "write", "critic"}
    for sp in steps:
        assert "cost_details" in sp.fields and "usage_details" in sp.fields, sp.name
        if sp.name in llm_steps:
            assert (
                sp.fields["usage_details"]["total"] > 0 and sp.fields["cost_details"]["total"] > 0
            )
    gens = [x for x in trace.spans if x.kind == "generation"]
    assert gens and all(g.fields["usage_details"] and g.model for g in gens)
    tools = [x for x in trace.spans if x.kind == "tool"]
    assert [t.name for t in tools] == ["tool.fetch_page"] and tools[0].parent.name == "research"  # type: ignore[union-attr]
    assert set(trace.scores) == {
        "icp_score", "faithfulness", "personalisation", "clarity", "tone", "code_check",
        "human_decision", "edit_distance",
    }  # fmt: skip
    assert any(t.startswith("run:") for t in trace.tags)
    lead = (await repo.list_leads(run))[0]
    assert lead.trace_id == trace.trace_id and lead.status == "outboxed"


async def test_non_fit_leads_stop_at_qualify_and_never_reach_research_or_approval(
    repo: Repository, settings: Settings, tracer: InMemoryTracer
) -> None:
    s, fakes = wire(settings)
    run = await new_run(repo, records(0, 6))
    report = await pipeline(repo, s).run(run)
    assert set(report.counts) <= {"not_fit", "maybe"} and fakes["writer"].calls == []
    assert await repo.drafts_with_status("pending_approval") == []
    names = {x.name for t in tracer.traces for x in t.step_spans()}
    assert names == {"extract", "qualify"}


async def test_t20_run_budget_stops_cleanly_and_no_partial_draft_reaches_approval(
    repo: Repository, settings: Settings, tracer: InMemoryTracer
) -> None:
    # lift the per-lead caps so this test exercises the PER-RUN dollar cap in isolation
    s, fakes = wire(
        settings,
        per_run_usd_cap=0.10,
        per_lead_usd_cap=10.0,
        per_lead_token_cap=10**7,
        concurrency=1,
    )
    big = writer_script

    def expensive(model: str, system: str, messages: Any, tools: Any) -> Any:
        out = big(model, system, messages, tools)
        if isinstance(out, str):
            return RawResult(
                text=out, input_tokens=60_000, output_tokens=10_000
            )  # ~$0.13 on GLM-5.3
        return out

    fakes["writer"].script = expensive
    run = await new_run(repo, records(3))
    report = await pipeline(repo, s).run(run)
    assert report.status == "budget_exceeded"
    assert (await repo.get_run(run)).status == "budget_exceeded"  # type: ignore[union-attr]
    assert report.counts == {"budget_exceeded": 3}
    assert await repo.drafts_with_status("pending_approval") == []
    assert await BotController(repo, s).push_pending() == []
    leads = await repo.list_leads(run)
    assert all(row.status == "budget_exceeded" for row in leads)


async def test_per_lead_budget_stops_only_that_lead(repo: Repository, settings: Settings) -> None:
    s, _ = wire(settings, per_lead_usd_cap=0.0000001)
    run = await new_run(repo, records(2))
    report = await pipeline(repo, s).run(run)
    assert report.counts == {"budget_exceeded": 2} and report.status == "completed"


async def test_e18_crash_resumes_from_last_finished_stage_without_repeating_work(
    repo: Repository, settings: Settings
) -> None:
    s, fakes = wire(settings)
    run = await new_run(repo, records(2))
    real = writer_script
    state = {"boom": True}

    def flaky(model: str, system: str, messages: Any, tools: Any) -> Any:
        if system.startswith("You write") and state["boom"]:
            raise RuntimeError("process died")
        return real(model, system, messages, tools)

    fakes["writer"].script = flaky
    r1 = await pipeline(repo, s).run(run)
    assert r1.counts == {"failed": 2}
    assert all(row.stage == "researched" for row in await repo.list_leads(run))
    qualify_calls = len([c for c in fakes["judge"].calls if c["system"].startswith("You qualify")])
    research_calls = len(
        [c for c in fakes["writer"].calls if c["system"].startswith("You research")]
    )

    state["boom"] = False
    r2 = await pipeline(repo, s).run(run)
    assert r2.counts == {"pending_approval": 2}
    after = fakes["judge"].calls + fakes["writer"].calls
    assert len([c for c in after if c["system"].startswith("You qualify")]) == qualify_calls
    assert len([c for c in after if c["system"].startswith("You research")]) == research_calls
    assert len(await repo.drafts_with_status("pending_approval")) == 2


async def test_one_failing_lead_does_not_kill_the_run(repo: Repository, settings: Settings) -> None:
    s, fakes = wire(settings)
    run = await new_run(repo, records(3))
    real = writer_script
    seen = {"n": 0}

    def one_bad(model: str, system: str, messages: Any, tools: Any) -> Any:
        if system.startswith("You write"):
            seen["n"] += 1
            if seen["n"] == 2:
                raise RuntimeError("provider exploded")
        return real(model, system, messages, tools)

    fakes["writer"].script = one_bad
    report = await pipeline(repo, s).run(run)
    assert report.counts == {"pending_approval": 2, "failed": 1}
    failed = [r for r in await repo.list_leads(run) if r.status == "failed"]
    assert "provider exploded" in (failed[0].skip_reason or "")


async def test_concurrency_is_bounded(repo: Repository, settings: Settings) -> None:
    limit = 3
    s, _ = wire(settings, concurrency=limit)
    live = {"now": 0, "max": 0}
    reached = asyncio.Event()

    class Gate:
        """Holds each call until `limit` are in flight at once (no wall-clock race)."""

        async def complete(
            self, model: str, system: str, messages: Any, tools: Any, mt: int
        ) -> RawResult:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
            if live["now"] >= limit:
                reached.set()
            try:
                await asyncio.wait_for(reached.wait(), timeout=5)
                await asyncio.sleep(0.05)  # stay in flight so a missing semaphore would overshoot
            finally:
                live["now"] -= 1
            out = judge_script(model, system, messages, tools)
            return RawResult(text=out) if isinstance(out, str) else out

    gateway = LLMGateway(
        {"judge": Gate(), "writer": FakeProvider(writer_script)},  # type: ignore[dict-item]
        s,
        backoff_base=0,
        routes=routes_for_fakes(),
    )
    llm.set_gateway(gateway)
    run = await new_run(repo, records(0, 10))  # non-fit leads: one (gated) qualify call each
    await pipeline(repo, s).run(run)
    assert live["max"] == limit  # parallelism really happens, and never exceeds the cap


async def test_rerun_of_a_finished_run_does_nothing(repo: Repository, settings: Settings) -> None:
    s, fakes = wire(settings)
    run = await new_run(repo, records(1))
    await pipeline(repo, s).run(run)
    n = len(fakes["writer"].calls) + len(fakes["judge"].calls)
    r2 = await pipeline(repo, s).run(run)
    assert r2.outcomes == [] and len(fakes["writer"].calls) + len(fakes["judge"].calls) == n


_ = json
