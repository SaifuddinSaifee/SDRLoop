"""Generate a challenger writer prompt from the latest human corrections."""

from __future__ import annotations

import json
from collections import Counter

from pydantic import BaseModel

from sdr_loop import llm
from sdr_loop.db.schema import EvalCase
from sdr_loop.prompts import PromptVersion, get_local

MAX_EXAMPLES = 8
MAX_GROWTH = 2.0  # a challenger may not balloon past 2x the champion's length
REQUIRED_PHRASES = ("fact", "claims")  # grounding rules must survive any rewrite


class Proposal(BaseModel):
    prompt: str
    rationale: str


def digest(cases: list[EvalCase]) -> dict[str, object]:
    """What the human keeps correcting: reject reasons plus before/after edit examples."""
    reasons = Counter(c.input.get("reason") for c in cases if c.source == "reject")
    edits = [
        c for c in cases if c.source == "edit" and c.expected and not c.expected.get("ungrounded")
    ]
    examples = [
        {
            "before": (c.input["original"]["email_1"]["body"])[:600],
            "after": (c.expected or {}).get("final_text", "")[:600],
        }
        for c in edits[:MAX_EXAMPLES]
    ]
    return {
        "reject_reasons": dict(reasons),
        "n_edits": sum(1 for c in cases if c.source == "edit"),
        "n_rejects": sum(1 for c in cases if c.source == "reject"),
        "edit_examples": examples,
    }


def sanity_check(champion: str, candidate: str) -> str | None:
    """Return why a candidate is unacceptable, or None. Cheap structural guardrails."""
    if not candidate.strip():
        return "empty prompt"
    if len(candidate) > MAX_GROWTH * len(champion):
        return f"too long ({len(candidate)} chars vs champion {len(champion)})"
    low = candidate.lower()
    missing = [p for p in REQUIRED_PHRASES if p not in low]
    if missing:
        return f"dropped grounding rules: missing {missing}"
    return None


async def propose_challenger(champion: PromptVersion, cases: list[EvalCase]) -> Proposal:
    meta = get_local("challenger")
    user = json.dumps(
        {"current_writer_prompt": champion.text, "human_corrections": digest(cases)},
        ensure_ascii=False,
    )
    res = await llm.complete(
        "challenger", meta.text, [{"role": "user", "content": user}],
        response_model=Proposal, max_tokens=3000,
    )  # fmt: skip
    proposal: Proposal = res.parsed
    return proposal
