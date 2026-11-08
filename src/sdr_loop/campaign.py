"""The campaign brief: who we are, what we offer, what we may claim, and the ask.

The writer, the critic and the qualifier read it as data in their user message, so the same
(learned) prompts work for any company. Claims about *us* are held to the same standard as claims
about the prospect: only `allowed_claims` may be stated; the critic flags anything else.

Edited on the control panel's Setup page; stored in `campaign.yaml`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class Sender(BaseModel):
    name: str = ""
    title: str = ""
    email: str = ""  # From: address (demo delivery goes to the local Mailpit inbox only)


class Campaign(BaseModel):
    company: str = ""
    website: str = ""
    one_liner: str = ""
    audience: str = ""
    pain: str = ""
    value: str = ""
    allowed_claims: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    goal: str = ""
    ask: str = ""
    tone: str = ""
    sender: Sender = Field(default_factory=Sender)
    search_urls: list[str] = Field(default_factory=list)  # Sales Navigator searches to run

    def missing(self) -> list[str]:
        """Fields the writer cannot do without."""
        need = {
            "company": self.company, "one-liner": self.one_liner, "ask": self.ask,
            "sender name": self.sender.name, "allowed claims": self.allowed_claims,
        }  # fmt: skip
        return [k for k, v in need.items() if not v]

    def brief(self) -> dict[str, Any]:
        """What the models see (no search URLs: they are operator config, not copy)."""
        return {
            "company": self.company,
            "website": self.website,
            "what_we_do": self.one_liner,
            "who_its_for": self.audience,
            "problem_we_solve": self.pain,
            "value": self.value,
            "allowed_claims": self.allowed_claims,
            "never_claim": self.forbidden_claims,
            "goal": self.goal,
            "ask": self.ask,
            "tone": self.tone,
            "sign_off": {"name": self.sender.name, "title": self.sender.title},
        }


def load_campaign(path: Path | None = None) -> tuple[Campaign, str]:
    p = path or Path(_settings_path())
    raw = p.read_text() if p.exists() else ""
    c = Campaign.model_validate(yaml.safe_load(raw) or {})
    return c, "campaign-" + hashlib.sha256(raw.encode()).hexdigest()[:8]


def current_brief() -> dict[str, Any]:
    return load_campaign()[0].brief()


def _settings_path() -> str:
    from sdr_loop.config import get_settings

    return get_settings().campaign_path


def save_campaign(c: Campaign, path: Path | None = None) -> None:
    data = c.model_dump()
    (path or Path(_settings_path())).write_text(
        "# Campaign brief: edit on the control panel's Setup page.\n"
        + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    )
