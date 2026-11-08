"""Orchestration: one Langfuse trace per lead, one span per step, resumable by stage.

extract -> qualify -> research -> write -> critic   (this module)
        -> approve -> outbox                        (bot/controller.py, appended to the same trace)

Steps are idempotent by lead id: `leads.stage` records the last finished stage, so a crashed or
budget-stopped run resumes where it stopped and finished work is never repeated (E3, E18).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from sdr_loop import budget
from sdr_loop.campaign import load_campaign
from sdr_loop.config import Settings
from sdr_loop.memory import Repository
from sdr_loop.models import ICP, Lead
from sdr_loop.observability import get_tracer, lead_trace_seed, step_span
from sdr_loop.prompts import get_prompt
from sdr_loop.steps.critic import write_with_critic
from sdr_loop.steps.extract import ExtractReport, ingest, load_csv
from sdr_loop.steps.qualify import qualify_lead
from sdr_loop.steps.research import make_registry, research_lead

log = logging.getLogger(__name__)


@dataclass
class LeadOutcome:
    lead_id: int
    status: str
    verdict: str | None = None
    cost_usd: float = 0.0
    latency_s: float = 0.0
    error: str | None = None


@dataclass
class RunReport:
    run_id: int
    status: str
    extract: ExtractReport | None = None
    outcomes: list[LeadOutcome] = field(default_factory=list)
    cost_usd: float = 0.0

    @property
    def counts(self) -> Counter[str]:
        return Counter(o.status for o in self.outcomes)

    @property
    def qualified(self) -> int:
        return sum(1 for o in self.outcomes if o.verdict == "fit")

    @property
    def cost_per_qualified_lead(self) -> float | None:
        return self.cost_usd / self.qualified if self.qualified else None

    @property
    def p95_latency_s(self) -> float | None:
        xs = sorted(o.latency_s for o in self.outcomes if o.status == "pending_approval")
        return xs[min(len(xs) - 1, int(0.95 * len(xs)))] if xs else None


def lead_from_row(row: Any) -> Lead:
    return Lead(
        linkedin_url=row.linkedin_url, full_name=row.name, title=row.title, company=row.company,
        company_website=row.company_website, industry=row.industry, headcount=row.headcount,
        country=row.country, email=row.email, email_status=row.email_status,
    )  # fmt: skip


HostCheck = Callable[[str], Awaitable[bool]]


class Pipeline:
    def __init__(
        self,
        repo: Repository,
        settings: Settings,
        icp: ICP,
        icp_version: str,
        http: httpx.AsyncClient,
        host_is_public: HostCheck | None = None,
    ) -> None:
        self.repo, self.settings, self.icp, self.icp_version = repo, settings, icp, icp_version
        self.http, self.host_is_public = http, host_is_public
        self._run_exhausted = False

    # ------------------------------------------------------------------ run

    async def run(self, run_id: int, limit: int | None = None) -> RunReport:
        run_budget = budget.RunBudget(self.settings.per_run_usd_cap)
        budget.set_run_budget(run_budget)
        await self.repo.set_run_status(run_id, "running")
        ids = (await self.repo.runnable_lead_ids(run_id))[:limit]
        sem = asyncio.Semaphore(self.settings.concurrency)
        done = 0
        log.info("run %s: processing %d leads", run_id, len(ids))

        async def one(lead_id: int) -> LeadOutcome:
            nonlocal done
            async with sem:
                if self._run_exhausted:  # stop cleanly: don't start new leads
                    await self.repo.set_lead_progress(
                        lead_id, status="budget_exceeded", reason="run budget exhausted"
                    )
                    out = LeadOutcome(lead_id, "budget_exceeded", error="run budget exhausted")
                else:
                    out = await self.process_lead(lead_id, run_id)
            done += 1  # progress line the web app turns into a progress bar (ids only, no PII)
            log.info("lead %d/%d done: lead %s %s", done, len(ids), lead_id, out.status)
            return out

        outcomes = await asyncio.gather(*(one(i) for i in ids))
        status = "budget_exceeded" if self._run_exhausted else "completed"
        await self.repo.set_run_status(run_id, status, cost_usd=run_budget.spent_usd)
        get_tracer().flush()
        return RunReport(run_id, status, outcomes=list(outcomes), cost_usd=run_budget.spent_usd)

    # ----------------------------------------------------------------- lead

    async def process_lead(self, lead_id: int, run_id: int) -> LeadOutcome:
        s = self.settings
        lb = budget.LeadBudget(s.per_lead_usd_cap, s.per_lead_token_cap, s.max_tool_calls)
        budget.set_lead_budget(lb)  # per-task contextvar: asyncio.gather copies the context
        row = await self.repo.get_lead(lead_id)
        lead = lead_from_row(row)
        t0 = time.perf_counter()
        verdict: str | None = None
        tracer = get_tracer()
        with tracer.trace(
            "lead",
            seed=lead_trace_seed(lead_id),
            tags=[f"run:{run_id}", f"icp:{self.icp_version}"],
            metadata={
                "run_id": run_id,
                "icp_version": self.icp_version,
                "campaign_version": load_campaign()[1],
            },
        ) as tr:
            await self.repo.set_lead_trace(lead_id, tr.trace_id)
            try:
                await self.repo.set_lead_progress(lead_id, status="in_progress")
                stage = row.stage
                if stage == "":
                    with step_span("extract", input={"email_status": lead.email_status}) as sp:
                        sp.update(output={"stored": True, "company": lead.company})
                    with step_span("qualify") as sp:
                        q, ref = await qualify_lead(lead, self.icp)
                        await self.repo.save_qualification(lead_id, q, ref)
                        tr.score("icp_score", q.score)
                        sp.update(output=q.model_dump(), metadata={"prompt": ref})
                    verdict = q.verdict
                    stage = "qualified"
                    await self.repo.set_lead_progress(lead_id, stage=stage)
                    if q.verdict != "fit":  # FR2.4: maybe leads are logged, not researched
                        end = "not_fit" if q.verdict == "not_fit" else "maybe"
                        await self.repo.set_lead_progress(lead_id, status=end)
                        return LeadOutcome(lead_id, end, verdict, lb.spent_usd, _since(t0))
                else:
                    saved = await self.repo.get_qualification(lead_id)
                    verdict = saved.verdict if saved else None

                if stage == "qualified":
                    with step_span("research") as sp:
                        registry = make_registry(lead, self.http, self.host_is_public)
                        res = await research_lead(lead, registry)
                        await self.repo.save_facts(lead_id, res.facts)
                        sp.update(
                            output={"facts": len(res.facts), "dropped": res.dropped,
                                    "tools": res.tool_log, "budget_hit": res.budget_hit},
                        )  # fmt: skip
                    stage = "researched"
                    await self.repo.set_lead_progress(lead_id, stage=stage)

                if stage == "researched":
                    facts = await self.repo.get_facts(lead_id)
                    out = await write_with_critic(lead, facts, get_prompt("writer"))
                    sc = out.report.scores
                    if sc:
                        for name in ("faithfulness", "personalisation", "clarity", "tone"):
                            tr.score(name, getattr(sc, name))
                    tr.score("code_check", 1.0 if out.report.code.passed else 0.0)
                    # Only a finished draft is ever stored: no partial draft reaches approval (E11).
                    await self.repo.save_draft(
                        lead_id,
                        out.sequence,
                        prompt_version=out.prompt_ref,
                        flagged=out.flagged,
                        flag_reason=",".join(out.flags) or None,
                        critic=out.report.model_dump(),
                    )
                    await self.repo.set_lead_progress(
                        lead_id, stage="drafted", status="pending_approval"
                    )
                return LeadOutcome(lead_id, "pending_approval", verdict, lb.spent_usd, _since(t0))
            except budget.BudgetExceeded as e:
                if e.scope == "run":
                    self._run_exhausted = True
                await self.repo.set_lead_progress(lead_id, status="budget_exceeded", reason=str(e))
                return LeadOutcome(
                    lead_id, "budget_exceeded", verdict, lb.spent_usd, _since(t0), str(e)
                )
            except Exception as e:  # one bad lead must not kill the run
                log.exception("lead %s failed", lead_id)
                await self.repo.set_lead_progress(
                    lead_id, status="failed", reason=f"{type(e).__name__}: {e}"[:300]
                )
                return LeadOutcome(
                    lead_id, "failed", verdict, lb.spent_usd, _since(t0), f"{type(e).__name__}: {e}"
                )


def _since(t0: float) -> float:
    return time.perf_counter() - t0


async def start_fixture_run(
    repo: Repository, csv_path: Path, icp_version: str
) -> tuple[int, ExtractReport]:
    run_id = await repo.create_run(f"fixture:{csv_path.name}", icp_version)
    rep = await ingest(repo, run_id, load_csv(csv_path))
    await repo.set_run_status(run_id, "extracted")
    return run_id, rep
