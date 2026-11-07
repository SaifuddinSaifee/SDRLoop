"""Canned, rule-based model providers for tests and `--fake-llm` demo runs (no API keys needed).

These return plausible but FIXED outputs: they exercise the plumbing (tools, validation, critic,
approval, tracing), not model quality. Never use them to report quality numbers.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sdr_loop.config import ROUTES, ModelSpec, Route, Settings
from sdr_loop.llm import FakeProvider, LLMGateway, Message, RawResult, ToolCall, ToolSpec

TARGET_INDUSTRIES = {
    "Marketing & Advertising", "Staffing & Recruiting", "Outsourcing/Offshoring",
    "Business Consulting & Services", "Computer Software",
}  # fmt: skip


def _user_json(messages: list[Message]) -> dict[str, Any]:
    first = next(m for m in messages if m["role"] == "user")
    start = first["content"].find("{")
    data: dict[str, Any] = json.loads(first["content"][start:])
    return data


def judge_script(
    model: str, system: str, messages: list[Message], tools: list[ToolSpec] | None
) -> RawResult | str:
    if system.startswith("You qualify"):
        lead = _user_json(messages)["lead"]
        fit = lead.get("industry") in TARGET_INDUSTRIES
        return json.dumps(
            {
                "score": 90 if fit else 20,
                "verdict": "fit" if fit else "not_fit",
                "reasons": [f"{lead.get('industry') or 'unknown'} / {lead.get('title')}"],
            }
        )
    if system.startswith("You are a strict reviewer"):
        r = dict.fromkeys(["faithfulness", "personalisation", "clarity", "tone"], "canned")
        return json.dumps(
            {"faithfulness": 5, "personalisation": 4, "clarity": 5, "tone": 5,
             "reasons": r, "uncited_claims": []}
        )  # fmt: skip
    raise AssertionError(f"judge_script: unexpected prompt {system[:40]!r}")


def writer_script(
    model: str, system: str, messages: list[Message], tools: list[ToolSpec] | None
) -> RawResult | str:
    if system.startswith("You research"):
        used = any(m["role"] == "tool" for m in messages)
        if not used:
            site = _user_json(messages)["company_website"].rstrip("/") + "/"
            return RawResult(tool_calls=[ToolCall("t1", "fetch_page", {"url": site})])
        tool = next(m for m in messages if m["role"] == "tool")["content"]
        m = re.match(r'<untrusted_content source="([^"]+)">\n(.*)', tool, re.DOTALL)
        if not m:  # tool error: no facts
            return json.dumps({"facts": []})
        url, text = m.group(1), m.group(2)
        return json.dumps(
            {"facts": [{"source_url": url, "snippet": text[:80].strip(), "kind": "company"}]}
        )
    if system.startswith("You write a two-email"):
        payload = _user_json(messages)
        company = payload["lead"]["company"]
        has_fact = bool(payload["facts"])
        e1 = {
            "subject": f"Idea for {company}"[:70],
            "body": f"Hi, I saw what your team is building at {company}. We give lead-gen teams "
            "clean, verified emails from Sales Navigator exports. Open to a quick call this week?",
            "claims": [{"text": f"what your team is building at {company}", "fact_ids": ["f1"]}]
            if has_fact
            else [],
        }
        e2 = {
            "subject": "Re: quick idea",
            "body": "One more angle: every email is verified before export. Worth a short reply?",
            "claims": [],
        }
        return json.dumps({"email_1": e1, "email_2": e2})
    raise AssertionError(f"writer_script: unexpected prompt {system[:40]!r}")


# Which side of the fake pair answers each task. "writer" mirrors the writer model's tasks and
# "judge" the qualifier/critic, so tests can script them independently.
FAKE_SIDE = {"write": "writer", "research": "writer", "challenger": "writer",
             "qualify": "judge", "critic": "judge"}  # fmt: skip


def routes_for_fakes() -> dict[str, Route]:
    """The real route table (real model ids, so prices match) re-pointed at fake providers."""
    out = {}
    for task, r in ROUTES.items():
        side = FAKE_SIDE[task]
        fb = ModelSpec(side, r.fallback.model) if r.fallback else None
        out[task] = Route(ModelSpec(side, r.primary.model), fb)
    return out


def fake_gateway(settings: Settings, *, backoff_base: float = 0.0) -> LLMGateway:
    return LLMGateway(
        {"writer": FakeProvider(writer_script), "judge": FakeProvider(judge_script)},
        settings,
        backoff_base=backoff_base,
        routes=routes_for_fakes(),
    )
