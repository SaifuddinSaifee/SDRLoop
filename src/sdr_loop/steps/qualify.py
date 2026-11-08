"""Step 2: qualify. Hard rules in code first (no LLM call on rejection), then the LLM."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

from sdr_loop import llm
from sdr_loop.campaign import current_brief
from sdr_loop.models import ICP, Lead, Qualification
from sdr_loop.prompts import PromptVersion, get_prompt

FIT_MIN = 75
MAYBE_MIN = 50


def load_icp(path: Path) -> tuple[ICP, str]:
    """Returns the ICP and a short content hash used as `icp_version`."""
    raw = path.read_text()
    return ICP.model_validate(yaml.safe_load(raw)), hashlib.sha256(raw.encode()).hexdigest()[:10]


def _has_word(text: str, word: str) -> bool:
    """Whole-word match with optional plural, so 'intern' hits 'Interns' but not 'International'."""
    return re.search(rf"\b{re.escape(word)}s?\b", text, re.IGNORECASE) is not None


def _has_prefix(text: str, kw: str) -> bool:
    """Word-start match, so 'recruit' hits 'Recruiter' and 'ceo' hits 'CEO'."""
    return re.search(rf"\b{re.escape(kw)}", text, re.IGNORECASE) is not None


def _reject(reason: str, disqualifier: str | None = None) -> Qualification:
    return Qualification(
        score=0, verdict="not_fit", reasons=[reason], disqualifier=disqualifier, decided_by="rule"
    )


def rule_check(lead: Lead, icp: ICP) -> Qualification | None:
    """Return a not_fit Qualification if a hard rule fails, else None (LLM decides)."""
    company = lead.company.lower()
    for name in icp.disqualifier_companies:
        if name.lower() in company:
            return _reject(f"Company matches disqualifier list: {name}", name)
    if icp.countries and lead.country.upper() not in {c.upper() for c in icp.countries}:
        return _reject(f"Country {lead.country or 'unknown'} is outside the ICP")
    if lead.headcount is not None and not (
        icp.min_headcount <= lead.headcount <= icp.max_headcount
    ):
        return _reject(
            f"Headcount {lead.headcount} outside {icp.min_headcount}-{icp.max_headcount}"
        )
    if any(_has_word(lead.title, w) for w in icp.title_exclude):
        return _reject(f"Title '{lead.title}' is excluded")
    if icp.title_keywords and not any(_has_prefix(lead.title, k) for k in icp.title_keywords):
        return _reject(f"Title '{lead.title}' matches no target role")
    return None


def _verdict_for(score: int) -> str:
    return "fit" if score >= FIT_MIN else "maybe" if score >= MAYBE_MIN else "not_fit"


def lead_payload(lead: Lead) -> dict[str, object]:
    """What the model sees. No email or LinkedIn URL: not needed to judge fit."""
    return {
        "title": lead.title,
        "company": lead.company,
        "industry": lead.industry,
        "headcount": lead.headcount,
        "country": lead.country,
        "company_website": lead.company_website,
    }


async def qualify_lead(
    lead: Lead, icp: ICP, prompt: PromptVersion | None = None
) -> tuple[Qualification, str]:
    """Returns (qualification, prompt_ref). prompt_ref is 'rules' when no LLM call was made."""
    if (rejected := rule_check(lead, icp)) is not None:
        return rejected, "rules"
    prompt = prompt or get_prompt("qualify")
    b = current_brief()
    offer = {k: b[k] for k in ("company", "what_we_do", "who_its_for")}
    user = json.dumps(
        {"OUR_OFFER": offer, "icp": icp.model_dump(), "lead": lead_payload(lead)},
        ensure_ascii=False,
    )
    res = await llm.complete(
        "qualify",
        prompt.text,
        [{"role": "user", "content": user}],
        response_model=Qualification,
        max_tokens=600,
    )
    q: Qualification = res.parsed
    # Score is the source of truth: keeps verdicts consistent and the thresholds testable.
    q = q.model_copy(update={"verdict": _verdict_for(q.score), "decided_by": "llm"})
    return q, prompt.ref
