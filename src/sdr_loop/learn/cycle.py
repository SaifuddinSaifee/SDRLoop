"""One nightly learning cycle: build cases, propose a challenger, compare, promote or archive."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sdr_loop import budget
from sdr_loop.config import Settings
from sdr_loop.db.schema import EvalCase, PromptPromotion
from sdr_loop.learn.build_eval_set import build_eval_cases, unused_cases
from sdr_loop.learn.challenger import propose_challenger, sanity_check
from sdr_loop.learn.golden import golden_fit_with_facts
from sdr_loop.learn.promote import (
    Item,
    decide,
    items_from_cases,
    items_from_golden,
    score_prompt,
)  # fmt: skip
from sdr_loop.memory import Repository
from sdr_loop.models import Fact, Lead
from sdr_loop.prompts import get_prompt, promote_prompt, save_challenger, version_number

log = logging.getLogger(__name__)
PROMPT = "writer"


@dataclass
class CycleResult:
    outcome: str  # skipped | archived | promoted | rejected_challenger
    reason: str = ""
    promoted: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


async def run_learning_cycle(
    repo: Repository,
    settings: Settings,
    golden: list[tuple[Lead, list[Fact]]] | None = None,
) -> CycleResult:
    if not settings.learn_enabled:
        return _skip("LEARN_ENABLED=false")
    if PROMPT in {p.strip() for p in settings.pinned_prompts.split(",") if p.strip()}:
        return _skip(f"prompt '{PROMPT}' is pinned by a human")

    await build_eval_cases(repo)
    cases = await unused_cases(repo)
    if len(cases) < settings.learn_min_new_cases:  # E16
        return _skip(f"only {len(cases)} new eval cases, need {settings.learn_min_new_cases}")

    budget.set_run_budget(budget.RunBudget(settings.per_run_usd_cap))
    champion = get_prompt(PROMPT, "production")
    proposal = await propose_challenger(champion, cases)
    if (problem := sanity_check(champion.text, proposal.prompt)) is not None:
        await _record(
            repo, champion, champion, 0.0, 0.0, False, {"rejected_candidate": problem}, cases
        )
        return CycleResult("rejected_challenger", problem)
    challenger = save_challenger(PROMPT, proposal.prompt)

    items: list[Item] = items_from_cases(cases) + items_from_golden(
        golden if golden is not None else golden_fit_with_facts()
    )
    champ_score = await score_prompt(champion, items, settings.concurrency)
    chall_score = await score_prompt(challenger, items, settings.concurrency)
    decision = decide(champ_score, chall_score)
    detail = {**decision.detail, "reasons": decision.reasons, "rationale": proposal.rationale}
    await _record(
        repo, champion, challenger, champ_score.avg(), chall_score.avg(),
        decision.promoted, detail, cases,
    )  # fmt: skip
    if decision.promoted:
        promote_prompt(challenger)
        return CycleResult("promoted", promoted=True, detail=detail)
    return CycleResult("archived", "; ".join(decision.reasons), detail=detail)


def _skip(reason: str) -> CycleResult:
    log.info("learn: skipped: %s", reason)
    return CycleResult("skipped", reason)


async def _record(
    repo: Repository,
    champion: Any,
    challenger: Any,
    champ: float,
    chall: float,
    promoted: bool,
    detail: dict[str, Any],
    cases: list[EvalCase],
) -> None:
    async with repo.session() as s:
        s.add(
            PromptPromotion(
                prompt_name=PROMPT,
                from_version=str(version_number(champion)),
                to_version=str(version_number(challenger)),
                champion_score=champ,
                challenger_score=chall,
                promoted=promoted,
                detail=detail,
            )
        )
        for c in cases:  # evaluated cases are consumed: the next night needs new corrections
            row = await s.get(EvalCase, c.id)
            if row is not None:
                row.used_in_promotion = True
        await s.commit()
