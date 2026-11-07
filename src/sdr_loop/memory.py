"""Repository layer over Postgres (SQLite in tests). All DB access goes through here."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sdr_loop.db.schema import (
    Base,
    DraftRow,
    FactRow,
    LeadRow,
    QualificationRow,
    Run,
    WebhookEvent,
)
from sdr_loop.models import Fact, Lead, Qualification, Sequence


class Repository:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> AsyncSession:
        return self._sessions()

    async def init_schema(self) -> None:
        """create_all: for tests and local SQLite. Postgres uses Alembic migrations."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    # ------------------------------------------------------------------ runs

    async def create_run(
        self, search_url: str, icp_version: str, extraction_id: str | None = None
    ) -> int:
        async with self.session() as s:
            run = Run(search_url=search_url, icp_version=icp_version, extraction_id=extraction_id)
            s.add(run)
            await s.commit()
            return run.id

    async def set_run_status(
        self, run_id: int, status: str, *, reason: str | None = None, cost_usd: float | None = None
    ) -> None:
        values: dict[str, object] = {"status": status}
        if reason is not None:
            values["failure_reason"] = reason
        if cost_usd is not None:
            values["cost_usd"] = cost_usd
        async with self.session() as s:
            await s.execute(update(Run).where(Run.id == run_id).values(**values))
            await s.commit()

    async def get_run(self, run_id: int) -> Run | None:
        async with self.session() as s:
            return await s.get(Run, run_id)

    async def get_run_by_extraction(self, extraction_id: str) -> Run | None:
        async with self.session() as s:
            res = await s.execute(select(Run).where(Run.extraction_id == extraction_id))
            return res.scalar_one_or_none()

    async def claim_webhook(self, extraction_id: str) -> bool:
        """True the first time an extraction id is seen, False on duplicates (E2)."""
        async with self.session() as s:
            s.add(WebhookEvent(extraction_id=extraction_id))
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return False
            return True

    # ----------------------------------------------------------------- leads

    async def upsert_lead(self, lead: Lead, run_id: int) -> tuple[int, bool]:
        """Insert by LinkedIn URL. Returns (lead_id, created). An existing lead is re-linked to
        the new run and keeps its state, so finished steps are skipped (E3)."""
        async with self.session() as s:
            res = await s.execute(select(LeadRow).where(LeadRow.linkedin_url == lead.linkedin_url))
            row = res.scalar_one_or_none()
            if row is not None:
                row.run_id = run_id
                await s.commit()
                return row.id, False
            row = LeadRow(
                linkedin_url=lead.linkedin_url,
                name=lead.full_name,
                title=lead.title,
                company=lead.company,
                company_website=lead.company_website,
                industry=lead.industry,
                headcount=lead.headcount,
                country=lead.country,
                email=lead.email,
                email_status=lead.email_status,
                run_id=run_id,
            )
            s.add(row)
            await s.commit()
            return row.id, True

    async def get_lead(self, lead_id: int) -> LeadRow:
        async with self.session() as s:
            row = await s.get(LeadRow, lead_id)
            if row is None:
                raise KeyError(f"lead {lead_id}")
            return row

    async def count_leads(self) -> int:
        async with self.session() as s:
            res = await s.execute(select(LeadRow.id))
            return len(res.all())

    async def list_leads(self, run_id: int | None = None) -> list[LeadRow]:
        async with self.session() as s:
            q = select(LeadRow).order_by(LeadRow.id)
            if run_id is not None:
                q = q.where(LeadRow.run_id == run_id)
            return list((await s.execute(q)).scalars())

    async def set_lead_status(
        self, lead_id: int, status: str, *, skip_reason: str | None = None
    ) -> None:
        values: dict[str, object] = {"status": status}
        if skip_reason is not None:
            values["skip_reason"] = skip_reason
        async with self.session() as s:
            await s.execute(update(LeadRow).where(LeadRow.id == lead_id).values(**values))
            await s.commit()

    async def set_lead_trace(self, lead_id: int, trace_id: str) -> None:
        async with self.session() as s:
            await s.execute(update(LeadRow).where(LeadRow.id == lead_id).values(trace_id=trace_id))
            await s.commit()

    # -------------------------------------------------------- qualifications

    async def save_qualification(
        self, lead_id: int, q: Qualification, prompt_version: str = ""
    ) -> None:
        async with self.session() as s:
            row = await s.get(QualificationRow, lead_id)
            if row is None:
                row = QualificationRow(lead_id=lead_id)
                s.add(row)
            row.score = q.score
            row.verdict = q.verdict
            row.reasons = list(q.reasons)
            row.disqualifier = q.disqualifier
            row.decided_by = q.decided_by
            row.prompt_version = prompt_version
            await s.commit()

    async def get_qualification(self, lead_id: int) -> Qualification | None:
        async with self.session() as s:
            row = await s.get(QualificationRow, lead_id)
            if row is None:
                return None
            return Qualification(
                score=row.score,
                verdict=row.verdict,
                reasons=row.reasons,
                disqualifier=row.disqualifier,
                decided_by=row.decided_by,
            )

    # ----------------------------------------------------------------- facts

    async def save_facts(self, lead_id: int, facts: list[Fact]) -> None:
        """Replace the lead's facts (idempotent re-research)."""
        async with self.session() as s:
            await s.execute(delete(FactRow).where(FactRow.lead_id == lead_id))
            for f in facts:
                s.add(
                    FactRow(
                        id=f.id,
                        lead_id=lead_id,
                        source_url=f.source_url,
                        snippet=f.snippet,
                        kind=f.kind,
                    )
                )
            await s.commit()

    async def get_facts(self, lead_id: int) -> list[Fact]:
        async with self.session() as s:
            rows = (
                await s.execute(
                    select(FactRow).where(FactRow.lead_id == lead_id).order_by(FactRow.pk)
                )
            ).scalars()
            return [
                Fact(id=r.id, source_url=r.source_url, snippet=r.snippet, kind=r.kind) for r in rows
            ]

    # ---------------------------------------------------------------- drafts

    async def save_draft(
        self,
        lead_id: int,
        seq: Sequence,
        *,
        prompt_version: str,
        flagged: bool = False,
        flag_reason: str | None = None,
        critic: dict[str, Any] | None = None,
        status: str = "pending_approval",
    ) -> int:
        cited = sorted(
            {fid for e in (seq.email_1, seq.email_2) for c in e.claims for fid in c.fact_ids}
        )
        async with self.session() as s:
            row = (
                await s.execute(select(DraftRow).where(DraftRow.lead_id == lead_id))
            ).scalar_one_or_none()
            if row is None:
                row = DraftRow(lead_id=lead_id, email_1={}, email_2={})
                s.add(row)
            row.email_1 = seq.email_1.model_dump()
            row.email_2 = seq.email_2.model_dump()
            row.cited_fact_ids = cited
            row.prompt_version = prompt_version
            row.critic_scores = critic
            row.status = status
            row.flagged = flagged
            row.flag_reason = flag_reason
            await s.commit()
            return row.id

    async def get_draft(self, lead_id: int) -> DraftRow | None:
        async with self.session() as s:
            return (
                await s.execute(select(DraftRow).where(DraftRow.lead_id == lead_id))
            ).scalar_one_or_none()
