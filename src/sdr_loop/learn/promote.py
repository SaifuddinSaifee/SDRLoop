"""Champion vs challenger on the same sets, and the promotion decision.

Promote only if ALL hold (PRD "Self-improving loop" + risk mitigations):
  1. zero invalid fact citations from the challenger (hard gate, whatever its other scores)
  2. challenger average score beats the champion's
  3. faithfulness rate does not drop
  4. regressions (cases where it scores clearly worse) stay at or below 5%
  5. it does not lose on the fixed golden set (guards against overfitting to recent edits)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz.distance import Levenshtein

from sdr_loop.models import Email, Fact, Lead, Sequence
from sdr_loop.prompts import PromptVersion
from sdr_loop.steps.approve import sequence_text
from sdr_loop.steps.critic import _words, code_checks, faithfulness, llm_critic
from sdr_loop.steps.write import write_sequence

REGRESSION_EPS = 0.05
MAX_REGRESSION_RATE = 0.05
W_CRITIC, W_SIMILARITY = 0.6, 0.4
TOO_LONG_WORDS = 75


@dataclass
class Item:
    """One thing a prompt is asked to write for."""

    key: str  # 'case:12' | 'golden:3'
    set_name: str  # 'cases' | 'golden'
    lead: Lead
    facts: list[Fact]
    expected_text: str | None = None
    reject_reason: str | None = None


@dataclass
class CaseScore:
    key: str
    set_name: str
    score: float
    valid_claims: int
    total_claims: int
    repeated_rejection: bool = False


@dataclass
class PromptScore:
    cases: list[CaseScore] = field(default_factory=list)

    def avg(self, set_name: str | None = None) -> float:
        xs = [c.score for c in self.cases if set_name in (None, c.set_name)]
        return sum(xs) / len(xs) if xs else 0.0

    @property
    def invalid_citations(self) -> int:
        return sum(c.total_claims - c.valid_claims for c in self.cases)

    @property
    def faithfulness_rate(self) -> float:
        total = sum(c.total_claims for c in self.cases)
        return 1.0 if total == 0 else sum(c.valid_claims for c in self.cases) / total


async def score_item(prompt: PromptVersion, item: Item) -> CaseScore:
    seq, _ = await write_sequence(item.lead, item.facts, prompt)  # raw prompt quality: one attempt
    valid, total = faithfulness(seq, item.facts)
    base = 0.0
    tone = faith = 5  # critic not run (code checks failed): nothing to hold against the prompt
    if code_checks(seq, item.facts).passed:
        scores = await llm_critic(item.lead, item.facts, seq)
        base, tone, faith = scores.average / 5, scores.tone, scores.faithfulness
    repeated = _repeats_rejection(item.reject_reason, seq, tone, faith)
    score = base
    if item.expected_text is not None and base > 0:
        sim = Levenshtein.normalized_similarity(sequence_text(seq), item.expected_text)
        score = W_CRITIC * base + W_SIMILARITY * sim
    if repeated:
        score = 0.0  # negative case: the new prompt must not repeat the rejected behaviour
    return CaseScore(item.key, item.set_name, score, valid, total, repeated)


def _repeats_rejection(reason: str | None, seq: Sequence, tone: int, faith: int) -> bool:
    if reason == "too_long":  # pure length check: needs no critic
        return _words(seq.email_1.body) > TOO_LONG_WORDS
    if reason == "wrong_fact":
        return faith < 4
    if reason == "bad_tone":
        return tone < 4
    return False


async def score_prompt(
    prompt: PromptVersion, items: list[Item], concurrency: int = 5
) -> PromptScore:
    sem = asyncio.Semaphore(concurrency)

    async def one(item: Item) -> CaseScore:
        async with sem:
            return await score_item(prompt, item)

    return PromptScore(list(await asyncio.gather(*(one(i) for i in items))))


@dataclass
class Decision:
    promoted: bool
    reasons: list[str]
    detail: dict[str, Any]


def decide(champion: PromptScore, challenger: PromptScore) -> Decision:
    by_key = {c.key: c for c in champion.cases}
    regress = [
        c.key for c in challenger.cases
        if c.key in by_key and c.score < by_key[c.key].score - REGRESSION_EPS
    ]  # fmt: skip
    n = max(1, len(challenger.cases))
    detail: dict[str, Any] = {
        "champion_avg": champion.avg(), "challenger_avg": challenger.avg(),
        "champion_golden": champion.avg("golden"), "challenger_golden": challenger.avg("golden"),
        "champion_cases": champion.avg("cases"), "challenger_cases": challenger.avg("cases"),
        "champion_faithfulness": champion.faithfulness_rate,
        "challenger_faithfulness": challenger.faithfulness_rate,
        "challenger_invalid_citations": challenger.invalid_citations,
        "regression_rate": len(regress) / n, "regressed": regress[:20],
        "n_items": len(challenger.cases),
    }  # fmt: skip
    fails = []
    if challenger.invalid_citations > 0:
        fails.append(f"hard gate: {challenger.invalid_citations} invalid fact citation(s)")
    if not challenger.avg() > champion.avg():
        fails.append("challenger average does not beat champion")
    if challenger.faithfulness_rate < champion.faithfulness_rate:
        fails.append("faithfulness dropped")
    if len(regress) / n > MAX_REGRESSION_RATE:
        fails.append(
            f"regressed on {len(regress) / n:.0%} of cases (max {MAX_REGRESSION_RATE:.0%})"
        )
    if challenger.avg("golden") < champion.avg("golden"):
        fails.append("loses on the golden set")
    return Decision(not fails, fails, detail)


def items_from_cases(cases: list[Any]) -> list[Item]:
    out = []
    for c in cases:
        lead_d = c.input["lead"]
        lead = Lead(
            linkedin_url="", full_name=lead_d["full_name"], title=lead_d["title"],
            company=lead_d["company"], industry=lead_d["industry"], country=lead_d["country"],
        )  # fmt: skip
        out.append(
            Item(
                key=f"case:{c.id}", set_name="cases", lead=lead,
                facts=[Fact(**f) for f in c.input["facts"]],
                expected_text=(c.expected or {}).get("final_text")
                if not (c.expected or {}).get("ungrounded")  # E14: never learn "grounded" from it
                else None,
                reject_reason=c.input.get("reason") if c.source == "reject" else None,
            )
        )  # fmt: skip
    return out


def items_from_golden(golden: list[tuple[Lead, list[Fact]]]) -> list[Item]:
    return [Item(f"golden:{i}", "golden", lead, facts) for i, (lead, facts) in enumerate(golden)]


__all__ = ["Decision", "Email", "Item", "PromptScore", "decide", "score_prompt"]
