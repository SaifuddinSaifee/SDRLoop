"""Step 4: write the two-email sequence. Every personal claim cites fact ids (checked in critic)."""

from __future__ import annotations

import json

from sdr_loop import llm
from sdr_loop.campaign import current_brief
from sdr_loop.models import Fact, Lead, Sequence
from sdr_loop.prompts import PromptVersion, get_prompt

ENGLISH_MARKETS = {"US", "GB", "CA", "IE", "AU", "NZ", "SG"}


def flags_for(lead: Lead, facts: list[Fact]) -> list[str]:
    flags = []
    if not facts:
        flags.append("no_facts")  # E5/E7: role-based email, human must look
    if lead.country and lead.country.upper() not in ENGLISH_MARKETS:
        flags.append("non_english_market")  # E17: v1 writes English only
    return flags


def writer_input(
    lead: Lead,
    facts: list[Fact],
    revision_notes: str | None = None,
    about_us: dict[str, object] | None = None,
) -> str:
    payload: dict[str, object] = {
        "ABOUT_US": about_us if about_us is not None else current_brief(),
        "lead": {
            "name": lead.full_name,
            "title": lead.title,
            "company": lead.company,
            "industry": lead.industry,
        },
        "facts": [{"id": f.id, "kind": f.kind, "snippet": f.snippet} for f in facts],
    }
    if revision_notes:
        payload["revision_notes"] = (
            "A reviewer rejected your previous draft. Fix these problems:\n" + revision_notes
        )
    return json.dumps(payload, ensure_ascii=False)


async def write_sequence(
    lead: Lead,
    facts: list[Fact],
    prompt: PromptVersion | None = None,
    revision_notes: str | None = None,
) -> tuple[Sequence, str]:
    prompt = prompt or get_prompt("writer")
    res = await llm.complete(
        "write",
        prompt.text,
        [{"role": "user", "content": writer_input(lead, facts, revision_notes)}],
        response_model=Sequence,
        max_tokens=1200,
    )
    return res.parsed, prompt.ref
