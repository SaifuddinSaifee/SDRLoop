"""Repository layer over Postgres (SQLite in tests). All DB access goes through here."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from sdr_loop.db.schema import (
    Base,
    DecisionRow,
    DraftRevision,
    DraftRow,
    EvalCase,
    FactRow,
    LeadRow,
    QualificationRow,
    Run,
    SendRow,
    WebhookEvent,
)
from sdr_loop.models import Fact, Lead, Qualification, Sequence


class Repository:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url)
        if url.startswith("sqlite"):  # local/test mode: WAL avoids an fsync on every commit

            @event.listens_for(self.engine.sync_engine, "connect")
            def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()

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

    # ------------------------------------------------------------- pipeline

    async def set_lead_progress(
        self,
        lead_id: int,
        *,
        stage: str | None = None,
        status: str | None = None,
        reason: str | None = None,
    ) -> None:
        values: dict[str, object] = {}
        if stage is not None:
            values["stage"] = stage
        if status is not None:
            values["status"] = status
        if reason is not None:
            values["skip_reason"] = reason
        if values:
            async with self.session() as s:
                await s.execute(update(LeadRow).where(LeadRow.id == lead_id).values(**values))
                await s.commit()

    async def set_run_extraction(self, run_id: int, extraction_id: str) -> None:
        async with self.session() as s:
            await s.execute(update(Run).where(Run.id == run_id).values(extraction_id=extraction_id))
            await s.commit()

    async def runnable_lead_ids(self, run_id: int) -> list[int]:
        """Leads of the run that still need work (new, in progress, or stopped by a budget)."""
        async with self.session() as s:
            q = (
                select(LeadRow.id)
                .where(LeadRow.run_id == run_id)
                .where(LeadRow.status.in_(("new", "in_progress", "budget_exceeded", "failed")))
                .order_by(LeadRow.id)
            )
            return list((await s.execute(q)).scalars())

    # ------------------------------------------------------------- approvals

    async def get_draft_by_id(self, draft_id: int) -> DraftRow:
        async with self.session() as s:
            row = await s.get(DraftRow, draft_id)
            if row is None:
                raise KeyError(f"draft {draft_id}")
            return row

    async def mark_notified(self, draft_id: int) -> None:
        async with self.session() as s:
            await s.execute(
                update(DraftRow)
                .where(DraftRow.id == draft_id)
                .values(notified_at=datetime.now(UTC))
            )
            await s.commit()

    async def drafts_with_status(
        self, status: str, *, unnotified: bool = False, older_than: datetime | None = None
    ) -> list[DraftRow]:
        async with self.session() as s:
            q = select(DraftRow).where(DraftRow.status == status).order_by(DraftRow.id)
            if unnotified:
                q = q.where(DraftRow.notified_at.is_(None))
            if older_than is not None:
                q = q.where(DraftRow.created_at < older_than)
            return list((await s.execute(q)).scalars())

    async def record_decision(
        self,
        draft_id: int,
        action: str,
        *,
        final_text: str | None = None,
        reason: str | None = None,
        edit_distance: int | None = None,
        uncited_human_claims: bool = False,
    ) -> int:
        """Store the decision; approve/edit move the draft to approved, reject to rejected."""
        status = "rejected" if action == "reject" else "approved"
        async with self.session() as s:
            d = DecisionRow(
                draft_id=draft_id,
                action=action,
                final_text=final_text,
                reason=reason,
                edit_distance=edit_distance,
                uncited_human_claims=uncited_human_claims,
            )
            s.add(d)
            await s.execute(update(DraftRow).where(DraftRow.id == draft_id).values(status=status))
            await s.commit()
            return d.id

    async def get_decision(self, draft_id: int) -> DecisionRow | None:
        async with self.session() as s:
            q = (
                select(DecisionRow)
                .where(DecisionRow.draft_id == draft_id)
                .order_by(DecisionRow.id.desc())
            )
            return (await s.execute(q)).scalars().first()

    async def set_draft_status(self, draft_id: int, status: str) -> None:
        async with self.session() as s:
            await s.execute(update(DraftRow).where(DraftRow.id == draft_id).values(status=status))
            await s.commit()

    # --------------------------------------------------------------- privacy

    async def find_lead(self, ident: str | int) -> LeadRow | None:
        """By numeric id or LinkedIn URL."""
        from sdr_loop.steps.extract import normalise_linkedin_url

        async with self.session() as s:
            if isinstance(ident, int) or str(ident).isdigit():
                return await s.get(LeadRow, int(ident))
            url = normalise_linkedin_url(str(ident))
            return (
                await s.execute(select(LeadRow).where(LeadRow.linkedin_url == url))
            ).scalar_one_or_none()

    async def purge_lead_rows(self, lead_id: int) -> dict[str, int]:
        """Delete a lead and everything derived from it, including eval cases built from its
        decisions. Aggregates (run cost, prompt promotions) hold no personal data and stay."""
        counts: dict[str, int] = {}
        async with self.session() as s:
            draft_ids = list(
                (await s.execute(select(DraftRow.id).where(DraftRow.lead_id == lead_id))).scalars()
            )
            decision_ids = list(
                (
                    await s.execute(
                        select(DecisionRow.id).where(DecisionRow.draft_id.in_(draft_ids))
                    )
                ).scalars()
            ) if draft_ids else []  # fmt: skip
            steps: list[tuple[str, Any]] = [
                ("sends", delete(SendRow).where(SendRow.lead_id == lead_id)),
                ("eval_cases", delete(EvalCase).where(EvalCase.decision_id.in_(decision_ids))),
                ("decisions", delete(DecisionRow).where(DecisionRow.draft_id.in_(draft_ids))),
                ("draft_revisions",
                 delete(DraftRevision).where(DraftRevision.draft_id.in_(draft_ids))),
                ("drafts", delete(DraftRow).where(DraftRow.lead_id == lead_id)),
                ("facts", delete(FactRow).where(FactRow.lead_id == lead_id)),
                ("qualifications", delete(QualificationRow).where(
                    QualificationRow.lead_id == lead_id)),
                ("leads", delete(LeadRow).where(LeadRow.id == lead_id)),
            ]  # fmt: skip
            for name, stmt in steps:
                counts[name] = int(getattr(await s.execute(stmt), "rowcount", 0) or 0)
            await s.commit()
        return counts

    # ------------------------------------------------------------- sending (demo inbox)

    async def schedule_send(
        self, draft_id: int, lead_id: int, step: int, to_email: str, subject: str, body: str,
        due_at: datetime,
    ) -> bool:  # fmt: skip
        """False if this email was already scheduled (unique draft+step)."""
        async with self.session() as s:
            row = SendRow(
                draft_id=draft_id, lead_id=lead_id, step=step, to_email=to_email,
                subject=subject, body=body, due_at=due_at, status="scheduled",
            )  # fmt: skip
            s.add(row)
            try:
                await s.commit()
            except IntegrityError:
                await s.rollback()
                return False
        return True

    async def due_sends(self, now: datetime) -> list[SendRow]:
        async with self.session() as s:
            q = (
                select(SendRow)
                .where(SendRow.status == "scheduled", SendRow.due_at <= now)
                .order_by(SendRow.due_at, SendRow.step)
            )
            return list((await s.execute(q)).scalars())

    async def claim_send(self, send_id: int) -> bool:
        """Atomically move scheduled -> sending, so two dispatchers never send the same row."""
        async with self.session() as s:
            res = await s.execute(
                update(SendRow)
                .where(SendRow.id == send_id, SendRow.status == "scheduled")
                .values(status="sending")
            )
            await s.commit()
            return bool(getattr(res, "rowcount", 0))

    async def finish_send(
        self,
        send_id: int,
        *,
        message_id: str | None = None,
        error: str | None = None,
        retry_at: datetime | None = None,
    ) -> None:
        """Sent, failed for good, or (with `retry_at`) back in the queue, e.g. inbox not up yet."""
        values: dict[str, Any]
        if error and retry_at:
            values = {"status": "scheduled", "error": error[:500], "due_at": retry_at}
        elif error:
            values = {"status": "failed", "error": error[:500]}
        else:
            values = {"status": "sent", "sent_at": datetime.now(UTC), "message_id": message_id,
                      "error": None}  # fmt: skip
        async with self.session() as s:
            await s.execute(update(SendRow).where(SendRow.id == send_id).values(**values))
            await s.commit()

    async def get_send(self, draft_id: int, step: int) -> SendRow | None:
        async with self.session() as s:
            q = select(SendRow).where(SendRow.draft_id == draft_id, SendRow.step == step)
            return (await s.execute(q)).scalars().first()

    async def list_sends(self) -> list[SendRow]:
        async with self.session() as s:
            return list((await s.execute(select(SendRow).order_by(SendRow.id.desc()))).scalars())

    async def reset_all(self, *, keep_learning: bool = False) -> dict[str, int]:
        """Delete every lead, run and derived row (fresh start). `keep_learning` keeps eval cases
        and promotion history."""
        from sdr_loop.db.schema import PromptPromotion

        tables: list[Any] = [SendRow]
        if not keep_learning:
            tables += [EvalCase, PromptPromotion]
        else:  # eval cases point at decisions: detach them before decisions go
            async with self.session() as s:
                await s.execute(update(EvalCase).values(decision_id=None))
                await s.commit()
        tables += [
            DecisionRow,
            DraftRevision,
            DraftRow,
            FactRow,
            QualificationRow,
            LeadRow,
            WebhookEvent,
            Run,
        ]
        counts: dict[str, int] = {}
        async with self.session() as s:
            for t in tables:
                res = await s.execute(delete(t))
                counts[t.__tablename__] = int(getattr(res, "rowcount", 0) or 0)
            await s.commit()
        return counts

    # ------------------------------------------------------------- revisions (edit by instruction)

    async def add_revision(
        self, draft_id: int, instruction: str, before: dict[str, Any], after: dict[str, Any]
    ) -> int:
        async with self.session() as s:
            q = select(DraftRevision.id).where(DraftRevision.draft_id == draft_id)
            n = len((await s.execute(q)).all()) + 1
            s.add(
                DraftRevision(
                    draft_id=draft_id, n=n, instruction=instruction, before=before, after=after
                )
            )
            await s.commit()
            return n

    async def list_revisions(self, draft_id: int) -> list[DraftRevision]:
        async with self.session() as s:
            q = select(DraftRevision).where(DraftRevision.draft_id == draft_id)
            return list((await s.execute(q.order_by(DraftRevision.n))).scalars())
