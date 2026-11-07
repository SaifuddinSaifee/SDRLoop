"""T10 + writing quality on the golden set (real models). Faithfulness is a HARD gate."""

from __future__ import annotations

import asyncio

import pytest

from evals.golden import golden_fit_with_facts
from sdr_loop.steps.critic import DraftOutcome, faithfulness, write_with_critic

CODE_PASS_MIN = 0.8
CRITIC_AVG_MIN = 3.5


async def run_all() -> list[tuple[str, DraftOutcome, tuple[int, int]]]:
    sem = asyncio.Semaphore(5)

    async def one(lead, facts):  # type: ignore[no-untyped-def]
        async with sem:
            out = await write_with_critic(lead, facts)
        return lead.company, out, faithfulness(out.sequence, facts)

    return list(await asyncio.gather(*(one(lead, f) for lead, f in golden_fit_with_facts())))


@pytest.mark.eval
async def test_faithfulness_is_100_percent_and_quality_reported() -> None:
    results = await run_all()
    valid = sum(r[2][0] for r in results)
    total = sum(r[2][1] for r in results)
    code_pass = sum(1 for _, o, _ in results if o.report.code.passed) / len(results)
    avgs = [o.report.scores.average for _, o, _ in results if o.report.scores]
    critic_avg = sum(avgs) / len(avgs) if avgs else 0.0
    flagged = sum(1 for _, o, _ in results if o.flagged)
    print(
        f"write: n={len(results)} claims valid={valid}/{total} code_pass={code_pass:.0%} "
        f"critic_avg={critic_avg:.2f} flagged={flagged}"
    )
    bad = [(c, f) for c, _, f in results if f[0] != f[1]]
    assert valid == total, f"faithfulness gate failed: {bad}"  # hard gate
    assert code_pass >= CODE_PASS_MIN, f"code-check pass rate {code_pass:.0%}"
    assert critic_avg >= CRITIC_AVG_MIN, f"critic average {critic_avg:.2f}"
