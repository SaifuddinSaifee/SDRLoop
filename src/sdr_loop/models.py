"""Pydantic domain types shared across steps."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["fit", "maybe", "not_fit"]


class Lead(BaseModel):
    linkedin_url: str
    full_name: str
    title: str = ""
    company: str = ""
    company_website: str = ""
    industry: str = ""
    headcount: int | None = None
    country: str = ""
    email: str = ""
    email_status: str = ""


class ICP(BaseModel):
    name: str = "default"
    description: str = ""
    industries: list[str] = Field(default_factory=list)
    min_headcount: int = 1
    max_headcount: int = 100_000
    title_keywords: list[str] = Field(default_factory=list)
    title_exclude: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    disqualifier_companies: list[str] = Field(default_factory=list)
    disqualifier_keywords: list[str] = Field(default_factory=list)


class Qualification(BaseModel):
    score: int = Field(ge=0, le=100)
    verdict: Verdict
    reasons: list[str] = Field(default_factory=list, max_length=3)
    disqualifier: str | None = None
    decided_by: Literal["rule", "llm"] = "llm"


class Fact(BaseModel):
    id: str
    source_url: str
    snippet: str
    kind: Literal["company", "news", "lead"] = "company"


class Claim(BaseModel):
    text: str
    fact_ids: list[str] = Field(default_factory=list)


class Email(BaseModel):
    subject: str
    body: str
    claims: list[Claim] = Field(default_factory=list)


class Sequence(BaseModel):
    email_1: Email
    email_2: Email


class CriticScores(BaseModel):
    faithfulness: int = Field(ge=1, le=5)
    personalisation: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    tone: int = Field(ge=1, le=5)
    reasons: dict[str, str] = Field(default_factory=dict)
    uncited_claims: list[str] = Field(default_factory=list)

    @property
    def average(self) -> float:
        return (self.faithfulness + self.personalisation + self.clarity + self.tone) / 4


class CodeCheckResult(BaseModel):
    passed: bool
    failures: list[str] = Field(default_factory=list)


class CriticReport(BaseModel):
    code: CodeCheckResult
    scores: CriticScores | None = None  # None when code checks failed first

    @property
    def passed(self) -> bool:
        if not self.code.passed:
            return False
        return (
            self.scores is not None and self.scores.faithfulness >= 4 and self.scores.average >= 3.5
        )

    def notes(self) -> str:
        out = list(self.code.failures)
        if self.scores:
            out += [f"{k}: {v}" for k, v in self.scores.reasons.items()]
            out += [f"uncited claim: {c}" for c in self.scores.uncited_claims]
        return "\n".join(out)
