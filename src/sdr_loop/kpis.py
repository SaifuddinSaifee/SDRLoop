"""KPI queries behind the Streamlit KPI page (and usable from a notebook or the CLI)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select

from sdr_loop.db.schema import (
    DecisionRow,
    DraftRow,
    LeadRow,
    PromptPromotion,
    QualificationRow,
    Run,
)
from sdr_loop.memory import Repository
from sdr_loop.models import CriticReport


def _critic_passed(raw: dict[str, Any] | None) -> bool | None:
    if not raw:
        return None
    try:
        return CriticReport.model_validate(raw).passed
    except ValueError:
        return None


async def per_prompt_version(repo: Repository) -> list[dict[str, Any]]:
    """Approval rate (no edits), average edit distance and critic pass rate per writer prompt."""
    async with repo.session() as s:
        drafts = list((await s.execute(select(DraftRow))).scalars())
        decisions = {d.draft_id: d for d in (await s.execute(select(DecisionRow))).scalars()}
    agg: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"drafts": 0, "reviewed": 0, "approved_as_written": 0, "edit_distances": [],
                 "critic_pass": 0, "critic_total": 0}
    )  # fmt: skip
    for d in drafts:
        a = agg[d.prompt_version or "unknown"]
        a["drafts"] += 1
        if (passed := _critic_passed(d.critic_scores)) is not None:
            a["critic_total"] += 1
            a["critic_pass"] += int(passed)
        dec = decisions.get(d.id)
        if dec is None:
            continue
        a["reviewed"] += 1
        a["approved_as_written"] += int(dec.action == "approve")
        if dec.edit_distance is not None:
            a["edit_distances"].append(dec.edit_distance)
    rows = []
    for version, a in sorted(agg.items()):
        eds = a["edit_distances"]
        reviewed, critic_total = a["reviewed"], a["critic_total"]
        rows.append(
            {
                "prompt_version": version,
                "drafts": a["drafts"],
                "reviewed": reviewed,
                "approval_rate_no_edits": (
                    a["approved_as_written"] / reviewed if reviewed else None
                ),
                "avg_edit_distance": sum(eds) / len(eds) if eds else None,
                "critic_pass_rate": a["critic_pass"] / critic_total if critic_total else None,
            }
        )
    return rows


async def per_run(repo: Repository) -> list[dict[str, Any]]:
    """Cost and funnel per run; cost per qualified (fit) lead is the PRD's cost KPI."""
    async with repo.session() as s:
        runs = list((await s.execute(select(Run).order_by(Run.id))).scalars())
        pairs = (
            await s.execute(
                select(LeadRow.run_id, QualificationRow.verdict).join(
                    QualificationRow, QualificationRow.lead_id == LeadRow.id
                )
            )
        ).all()
    qualified: dict[int, int] = defaultdict(int)
    for run_id, verdict in pairs:
        if verdict == "fit" and run_id is not None:
            qualified[run_id] += 1
    return [
        {
            "run": r.id, "status": r.status, "started_at": r.started_at, "cost_usd": r.cost_usd,
            "qualified": qualified[r.id],
            "cost_per_qualified": r.cost_usd / qualified[r.id] if qualified[r.id] else None,
        }
        for r in runs
    ]  # fmt: skip


async def promotions(repo: Repository) -> list[dict[str, Any]]:
    async with repo.session() as s:
        rows = (await s.execute(select(PromptPromotion).order_by(PromptPromotion.id))).scalars()
        return [
            {
                "when": p.created_at, "prompt": p.prompt_name,
                "from": p.from_version, "to": p.to_version,
                "champion": p.champion_score, "challenger": p.challenger_score,
                "promoted": p.promoted, "why_not": "; ".join(p.detail.get("reasons", [])),
            }
            for p in rows
        ]  # fmt: skip
