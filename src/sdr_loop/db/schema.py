"""SQLAlchemy 2.0 ORM tables (PRD data model plus idempotency/flag columns)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    search_url: Mapped[str] = mapped_column(Text, default="")
    extraction_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    icp_version: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(32), default="created")
    failure_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_id: Mapped[str] = mapped_column(String(64), unique=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class LeadRow(Base):
    __tablename__ = "leads"
    id: Mapped[int] = mapped_column(primary_key=True)
    linkedin_url: Mapped[str] = mapped_column(String(512), unique=True)
    name: Mapped[str] = mapped_column(String(256), default="")
    title: Mapped[str] = mapped_column(String(256), default="")
    company: Mapped[str] = mapped_column(String(256), default="")
    company_website: Mapped[str] = mapped_column(String(512), default="")
    industry: Mapped[str] = mapped_column(String(256), default="")
    headcount: Mapped[int | None] = mapped_column(Integer)
    country: Mapped[str] = mapped_column(String(64), default="")
    email: Mapped[str] = mapped_column(String(256), default="")
    email_status: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32), default="new")  # outcome
    stage: Mapped[str] = mapped_column(String(16), default="")  # last completed stage (resume)
    skip_reason: Mapped[str | None] = mapped_column(Text)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id"))


class QualificationRow(Base):
    __tablename__ = "qualifications"
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), primary_key=True)
    score: Mapped[int] = mapped_column(Integer)
    verdict: Mapped[str] = mapped_column(String(16))
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    disqualifier: Mapped[str | None] = mapped_column(String(256))
    decided_by: Mapped[str] = mapped_column(String(8), default="llm")
    prompt_version: Mapped[str] = mapped_column(String(64), default="")


class FactRow(Base):
    __tablename__ = "facts"
    pk: Mapped[int] = mapped_column(primary_key=True)
    id: Mapped[str] = mapped_column(String(32))
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    source_url: Mapped[str] = mapped_column(Text)
    snippet: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(16), default="company")
    __table_args__ = (UniqueConstraint("lead_id", "id"),)


class DraftRow(Base):
    __tablename__ = "drafts"
    id: Mapped[int] = mapped_column(primary_key=True)
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"), unique=True)
    email_1: Mapped[dict[str, Any]] = mapped_column(JSON)
    email_2: Mapped[dict[str, Any]] = mapped_column(JSON)
    cited_fact_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    prompt_version: Mapped[str] = mapped_column(String(64), default="")
    critic_scores: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending_approval")
    flagged: Mapped[bool] = mapped_column(default=False)
    flag_reason: Mapped[str | None] = mapped_column(Text)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class DecisionRow(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"))
    action: Mapped[str] = mapped_column(String(16))  # approve | edit | reject
    final_text: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(String(64))
    edit_distance: Mapped[int | None] = mapped_column(Integer)
    uncited_human_claims: Mapped[bool] = mapped_column(default=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EvalCase(Base):
    __tablename__ = "eval_cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))  # edit | reject | reply | golden
    input: Mapped[dict[str, Any]] = mapped_column(JSON)
    expected: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"), unique=True)
    used_in_promotion: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PromptPromotion(Base):
    __tablename__ = "prompt_promotions"
    id: Mapped[int] = mapped_column(primary_key=True)
    prompt_name: Mapped[str] = mapped_column(String(64))
    from_version: Mapped[str] = mapped_column(String(32))
    to_version: Mapped[str] = mapped_column(String(32))
    champion_score: Mapped[float] = mapped_column(Float)
    challenger_score: Mapped[float] = mapped_column(Float)
    promoted: Mapped[bool] = mapped_column()
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SendRow(Base):
    """One email of an approved sequence. Step 1 is due on approval, step 2 after the follow-up
    delay. Unique (draft, step): a retry or restart can never send the same email twice."""

    __tablename__ = "sends"
    __table_args__ = (UniqueConstraint("draft_id", "step"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"))
    lead_id: Mapped[int] = mapped_column(ForeignKey("leads.id"))
    step: Mapped[int] = mapped_column(Integer)  # 1 | 2
    to_email: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="scheduled")  # scheduled|sent|failed
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
