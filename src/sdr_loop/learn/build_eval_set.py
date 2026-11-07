"""Turn human decisions into eval cases. Only edits and rejections teach the agent (PRD FR8.1)."""

from __future__ import annotations

from sqlalchemy import select

from sdr_loop.db.schema import DecisionRow, DraftRow, EvalCase
from sdr_loop.memory import Repository


async def build_eval_cases(repo: Repository) -> int:
    """Create an EvalCase for every edit/reject decision that has none yet. Returns how many."""
    created = 0
    async with repo.session() as s:
        decisions = (
            await s.execute(
                select(DecisionRow)
                .where(DecisionRow.action.in_(("edit", "reject")))
                .where(~DecisionRow.id.in_(select(EvalCase.decision_id).where(EvalCase.decision_id.is_not(None))))
                .order_by(DecisionRow.id)
            )
        ).scalars().all()  # fmt: skip
        for d in decisions:
            draft = await s.get(DraftRow, d.draft_id)
            assert draft is not None
            lead = await repo.get_lead(draft.lead_id)
            facts = await repo.get_facts(draft.lead_id)
            case_input = {
                "lead": {  # no email / LinkedIn URL: eval inputs are replayed through LLMs
                    "full_name": lead.name, "title": lead.title, "company": lead.company,
                    "industry": lead.industry, "country": lead.country,
                },
                "facts": [f.model_dump() for f in facts],
                "original": {"email_1": draft.email_1, "email_2": draft.email_2},
                "reason": d.reason,
                "prompt_version": draft.prompt_version,
            }  # fmt: skip
            expected = (
                {"final_text": d.final_text, "ungrounded": d.uncited_human_claims}
                if d.action == "edit"
                else None
            )
            s.add(EvalCase(source=d.action, input=case_input, expected=expected, decision_id=d.id))
            created += 1
        await s.commit()
    return created


async def unused_cases(repo: Repository) -> list[EvalCase]:
    async with repo.session() as s:
        q = select(EvalCase).where(EvalCase.used_in_promotion.is_(False)).order_by(EvalCase.id)
        return list((await s.execute(q)).scalars())
