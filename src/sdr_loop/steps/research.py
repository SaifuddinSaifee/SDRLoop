"""Step 3: research. The one free agent: a tool-using loop with a hard budget.

Safety model (FR3.4): tool output is wrapped as untrusted data, the toolset is frozen and
read-only, fetch_page is restricted to the lead's company domain, and every fact is
validated IN CODE against what the tools actually returned. Instructions in web text can
neither add tools nor reach the email unless they also survive fact validation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from sdr_loop import llm
from sdr_loop.budget import LeadBudget, current_lead_budget
from sdr_loop.config import get_settings
from sdr_loop.models import Fact, Lead
from sdr_loop.prompts import PromptVersion, get_prompt
from sdr_loop.tools.base import ToolRegistry, ToolResult, render_for_model

MAX_TURNS = 5
MAX_FACTS = 6
MIN_SNIPPET, MAX_SNIPPET = 15, 300
_WS = re.compile(r"\s+")
_INSTRUCTION_LIKE = re.compile(
    r"ignore (all |any )?(the )?(previous|prior|above)|disregard (all |the )?(previous|prior|above)"
    r"|system prompt|you are now|admin mode|as an ai\b|new instructions"
    r"|call (the )?(fetch_page|search_news)",
    re.IGNORECASE,
)


class RawFact(BaseModel):
    source_url: str
    snippet: str
    kind: Literal["company", "news", "lead"] = "company"


class ResearchOutput(BaseModel):
    facts: list[RawFact] = Field(default_factory=list)


@dataclass
class ResearchResult:
    facts: list[Fact] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # reasons, for traces/tests
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    budget_hit: bool = False


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def validate_facts(raw: list[RawFact], corpus: dict[str, str]) -> tuple[list[Fact], list[str]]:
    """Keep a fact only if its snippet is a verbatim substring of text that a tool returned for
    that very source URL. Drops instruction-like snippets as defence in depth."""
    norm_corpus = {u: _norm(t) for u, t in corpus.items()}
    kept: list[Fact] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for rf in raw:
        snip = _norm(rf.snippet)
        if not (MIN_SNIPPET <= len(snip) <= MAX_SNIPPET):
            dropped.append(f"length:{snip[:40]}")
        elif rf.source_url not in norm_corpus:
            dropped.append(f"unknown_source:{rf.source_url}")
        elif snip not in norm_corpus[rf.source_url]:
            dropped.append(f"not_in_source:{snip[:40]}")
        elif _INSTRUCTION_LIKE.search(snip):
            dropped.append(f"instruction_like:{snip[:40]}")
        elif snip in seen:
            dropped.append(f"duplicate:{snip[:40]}")
        else:
            seen.add(snip)
            kept.append(
                Fact(id=f"f{len(kept) + 1}", source_url=rf.source_url, snippet=snip, kind=rf.kind)
            )
        if len(kept) >= MAX_FACTS:
            break
    return kept, dropped


def _lead_brief(lead: Lead) -> str:
    return json.dumps(
        {
            "name": lead.full_name,
            "title": lead.title,
            "company": lead.company,
            "company_website": lead.company_website,
            "industry": lead.industry,
            "country": lead.country,
        }
    )


async def research_lead(
    lead: Lead,
    registry: ToolRegistry,
    prompt: PromptVersion | None = None,
) -> ResearchResult:
    prompt = prompt or get_prompt("research")
    messages: list[llm.Message] = [
        {"role": "user", "content": f"Research this lead. Lead: {_lead_brief(lead)}"}
    ]
    corpus: dict[str, str] = {}
    result = ResearchResult()
    final_text: str | None = None

    for _ in range(MAX_TURNS):
        res = await llm.complete(
            "research", prompt.text, messages, tools=registry.specs, max_tokens=1500
        )
        if not res.tool_calls:
            final_text = res.text
            break
        messages.append(
            {
                "role": "assistant",
                "content": res.text,
                "tool_calls": [
                    {"id": c.id, "name": c.name, "input": c.input} for c in res.tool_calls
                ],
            }
        )
        refused_any = False
        for call in res.tool_calls:
            tr, refused = await registry.execute(call)
            refused_any |= refused
            _collect(corpus, tr)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": render_for_model(tr)}
            )
        if refused_any:  # T7: refuse and end with the facts so far
            result.budget_hit = True
            break

    parsed: ResearchOutput | None = None
    if final_text is not None:
        try:
            parsed = ResearchOutput.model_validate(llm.extract_json(final_text))
        except (ValueError, TypeError):
            parsed = None
    if parsed is None:  # budget hit, turn limit, or unparseable answer: ask for the final JSON
        messages.append(
            {
                "role": "user",
                "content": "Stop using tools. Reply now with ONLY the final JSON object of facts "
                "gathered so far (empty list if none).",
            }
        )
        try:
            fin = await llm.complete(
                "research", prompt.text, messages, response_model=ResearchOutput, max_tokens=1500
            )
            parsed = fin.parsed
        except llm.LLMError:
            parsed = ResearchOutput()

    assert parsed is not None
    result.facts, result.dropped = validate_facts(parsed.facts, corpus)
    result.tool_log = registry.call_log
    return result


def _collect(corpus: dict[str, str], tr: ToolResult) -> None:
    if not tr.ok:
        return
    corpus[tr.source_url] = tr.text
    corpus.update(tr.sources)


def make_registry(lead: Lead, http: Any, host_is_public: Any = None) -> ToolRegistry:
    """Build the frozen read-only registry with a per-lead tool-call budget."""
    from sdr_loop.tools import FetchPage, ReadLead, SearchNews, ToolContext

    s = get_settings()
    lb = current_lead_budget() or LeadBudget(
        s.per_lead_usd_cap, s.per_lead_token_cap, s.max_tool_calls
    )
    ctx = ToolContext(lead=lead, http=http)
    if host_is_public is not None:
        ctx.host_is_public = host_is_public
    return ToolRegistry([FetchPage(), SearchNews(), ReadLead()], lb, ctx)
