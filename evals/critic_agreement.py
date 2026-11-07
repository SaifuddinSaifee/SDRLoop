"""Critic agreement: does the LLM critic agree with YOU? (PRD: run once on day 4, then weekly.)

  uv run python -m evals.critic_agreement export   # writes evals/critic_agreement.json (20 drafts)
  ... open the file and fill each "human" block with your 1-5 scores ...
  uv run python -m evals.critic_agreement compare

If agreement is low the critic cannot be trusted to promote prompts: switch the critic model.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

DIMS = ("faithfulness", "personalisation", "clarity", "tone")
PATH = Path(__file__).resolve().parent / "critic_agreement.json"
WITHIN1_MIN = 0.80
VERDICT_MIN = 0.80
N = 20


def passes(s: dict[str, Any]) -> bool:
    return s["faithfulness"] >= 4 and sum(s[d] for d in DIMS) / 4 >= 3.5


def agreement(rows: list[dict[str, Any]]) -> dict[str, Any]:
    exact = within1 = pairs = verdict = 0
    for r in rows:
        h, c = r["human"], r["critic"]
        for d in DIMS:
            pairs += 1
            exact += h[d] == c[d]
            within1 += abs(h[d] - c[d]) <= 1
        verdict += passes(h) == passes(c)
    out = {
        "drafts": len(rows),
        "exact": exact / pairs,
        "within_1": within1 / pairs,
        "verdict_agreement": verdict / len(rows),
    }
    out["trustworthy"] = out["within_1"] >= WITHIN1_MIN and out["verdict_agreement"] >= VERDICT_MIN
    return out


async def export() -> None:
    from evals.golden import golden_fit_with_facts
    from sdr_loop.steps.critic import llm_critic
    from sdr_loop.steps.write import write_sequence

    items = golden_fit_with_facts()[:N]
    rows = []
    for lead, facts in items:
        seq, _ = await write_sequence(lead, facts)
        sc = await llm_critic(lead, facts, seq)
        rows.append(
            {
                "lead": f"{lead.full_name}, {lead.title} at {lead.company}",
                "facts": {f.id: f.snippet for f in facts},
                "draft": seq.model_dump(),
                "critic": {d: getattr(sc, d) for d in DIMS},
                "human": dict.fromkeys(DIMS),
            }
        )
    PATH.write_text(json.dumps(rows, indent=2))
    print(f"wrote {len(rows)} drafts to {PATH}: fill each 'human' block, then run compare")


def compare() -> None:
    rows = json.loads(PATH.read_text())
    missing = [i for i, r in enumerate(rows) if any(r["human"][d] is None for d in DIMS)]
    if missing:
        sys.exit(f"fill in 'human' scores first (drafts {missing})")
    print(json.dumps(agreement(rows), indent=2))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "export":
        from sdr_loop.observability import init_tracer_from_settings

        init_tracer_from_settings()
        asyncio.run(export())
    elif len(sys.argv) > 1 and sys.argv[1] == "compare":
        compare()
    else:
        sys.exit(__doc__)
