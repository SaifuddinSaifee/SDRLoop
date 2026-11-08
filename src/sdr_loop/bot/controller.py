"""Approval bot logic, independent of the Telegram library so it is fully testable.

callback data:  a:<draft>  approve   e:<draft>  edit   r:<draft>  reject
                rr:<draft>:<reason>  reject with a one-tap reason
commands:       /run [N] | /run <Sales Navigator URL> [N] | /learn | /status | /help
Only TELEGRAM_CHAT_ID is ever answered (T15); everyone else is ignored and logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sdr_loop.config import Settings
from sdr_loop.jobs import DEFAULT_LIMIT, JobBusy, JobStore, run_args
from sdr_loop.memory import Repository
from sdr_loop.models import Lead
from sdr_loop.observability import get_tracer, lead_trace_seed, step_span
from sdr_loop.steps import approve as ap
from sdr_loop.steps.outbox import write_outbox

log = logging.getLogger(__name__)

Button = tuple[str, str]  # (label, callback_data)


@dataclass
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    force_reply: bool = False


class BotController:
    def __init__(self, repo: Repository, settings: Settings, jobs: JobStore | None = None) -> None:
        self.repo = repo
        self.settings = settings
        self.jobs = jobs
        self._editing: dict[str, int] = {}  # chat id -> draft id awaiting the edited text

    def authorised(self, chat_id: int | str) -> bool:
        allowed = self.settings.telegram_chat_id
        return bool(allowed) and str(chat_id) == str(allowed)

    # ------------------------------------------------------------------ outgoing

    async def push_pending(self) -> list[tuple[int, Reply]]:
        """Drafts waiting for review that were not sent yet. Marks them notified."""
        out = []
        for d in await self.repo.drafts_with_status("pending_approval", unnotified=True):
            lead = await self.repo.get_lead(d.lead_id)
            facts = await self.repo.get_facts(d.lead_id)
            text = ap.review_text(_lead(lead), d, facts)
            out.append((d.id, Reply(text, [_main_buttons(d.id)])))
            await self.repo.mark_notified(d.id)
        return out

    async def reminder(self, now: datetime | None = None) -> str | None:
        """E13: a daily nudge while drafts stay pending. Nothing is ever sent without approval."""
        cutoff = (now or datetime.now(UTC)) - timedelta(hours=24)
        old = await self.repo.drafts_with_status("pending_approval", older_than=cutoff)
        return f"⏰ {len(old)} draft(s) still waiting for your review." if old else None

    def announcements(self) -> list[str]:
        """One message per background job that ended since the last call."""
        if self.jobs is None:
            return []
        out = []
        for job in self.jobs.unannounced():
            quiet = job.source == "schedule" and job.state == "done" and "skipped" in job.summary()
            if not quiet:  # a nightly "not enough new cases" is not news
                icon = "✅" if job.state == "done" else "❌"
                out.append(f"{icon} {job.label}\n{job.summary()}")
            self.jobs.mark_announced(job)
        return out

    # ------------------------------------------------------------------ incoming

    async def on_command(self, chat_id: int | str, command: str, args: list[str]) -> Reply | None:
        if not self.authorised(chat_id):
            log.warning("ignored /%s from unauthorised chat %s", command, chat_id)
            return None
        if command == "status":
            return Reply(await self.status_text())
        if command in ("run", "learn"):
            if self.jobs is None:
                return Reply("Background jobs are off in this bot. Use `sdr-loop start`.")
            if command == "learn":
                cmd, label = ["learn"], "Learning cycle"
            else:
                parsed = _parse_run(args)
                if parsed is None:
                    return Reply(_RUN_USAGE)
                cmd, label = run_args(*parsed)
            try:
                job = self.jobs.start("learn" if command == "learn" else "run", cmd,
                                      label=label, source="telegram")  # fmt: skip
            except JobBusy as e:
                return Reply(f"⏳ {e} Try again when it finishes.")
            follow = "Drafts will arrive here as they're ready." if command == "run" else ""
            return Reply(f"🚀 Started: {job.label}. {follow}".strip())
        return Reply(_HELP)

    async def status_text(self) -> str:
        pending = await self.repo.drafts_with_status("pending_approval")
        lines = [f"📝 {len(pending)} draft(s) waiting for review."]
        if self.jobs is not None:
            if (cur := self.jobs.current()) is not None:
                lines.append(f"⏳ Running: {cur.label} (since {cur.started_at:%H:%M} UTC).")
            elif (last := self.jobs.latest()) is not None:
                lines.append(f"Last job: {last.label} ({last.state}).\n{last.summary()}")
        return "\n".join(lines)

    async def on_action(self, chat_id: int | str, data: str) -> Reply | None:
        if not self.authorised(chat_id):
            log.warning("ignored callback from unauthorised chat %s", chat_id)
            return None
        kind, _, rest = data.partition(":")
        draft_id = int(rest.split(":")[0])
        d = await self.repo.get_draft_by_id(draft_id)
        if d.status != "pending_approval":
            return Reply(f"Draft #{draft_id} was already {d.status}.")
        if kind == "a":
            await self.decide(draft_id, "approve")
            return Reply(f"✅ Draft #{draft_id} approved and written to the outbox (dry run).")
        if kind == "e":
            self._editing[str(chat_id)] = draft_id
            return Reply(
                f"Reply with the new text for draft #{draft_id}: email 1, a line with only ---, "
                "then email 2. An optional first line 'Subject: ...' sets each subject.",
                force_reply=True,
            )
        if kind == "r":
            return Reply(
                f"Why reject draft #{draft_id}?",
                [[(r.replace("_", " "), f"rr:{draft_id}:{r}") for r in ap.REJECT_REASONS[:3]],
                 [(r.replace("_", " "), f"rr:{draft_id}:{r}") for r in ap.REJECT_REASONS[3:]]],
            )  # fmt: skip
        if kind == "rr":
            await self.decide(draft_id, "reject", reason=rest.split(":")[1])
            return Reply(f"🗑 Draft #{draft_id} rejected. Thanks, this feeds the learning loop.")
        log.warning("unknown callback %r", data)
        return None

    async def on_text(self, chat_id: int | str, text: str) -> Reply | None:
        if not self.authorised(chat_id):
            log.warning("ignored message from unauthorised chat %s", chat_id)
            return None
        draft_id = self._editing.get(str(chat_id))
        if draft_id is None:
            return Reply(_HELP)
        try:
            await self.decide(draft_id, "edit", new_text=text)
        except ap.EditFormatError as e:
            return Reply(f"Could not parse that: {e}", force_reply=True)
        del self._editing[str(chat_id)]
        return Reply(
            f"✏️ Draft #{draft_id} saved with your edit and written to the outbox (dry run)."
        )

    # ------------------------------------------------------------------ decisions

    async def decide(
        self, draft_id: int, action: str, *, reason: str | None = None, new_text: str | None = None
    ) -> None:
        """Record the human decision, then (approve/edit only) write the outbox. Both are steps of
        the lead's original trace, so the 7 step spans live in one trace."""
        d = await self.repo.get_draft_by_id(draft_id)
        tracer = get_tracer()
        with tracer.trace("lead", seed=lead_trace_seed(d.lead_id)) as tr:
            with step_span("approve", input={"draft_id": draft_id}) as sp:
                if action == "approve":
                    await ap.approve(self.repo, draft_id)
                elif action == "edit":
                    assert new_text is not None
                    facts = await self.repo.get_facts(d.lead_id)
                    await ap.edit(self.repo, draft_id, new_text, facts)
                else:
                    await ap.reject(self.repo, draft_id, reason or "other")
                decision = await self.repo.get_decision(draft_id)
                assert decision is not None
                tr.score("human_decision", action)
                if decision.edit_distance is not None:
                    tr.score("edit_distance", float(decision.edit_distance))
                sp.update(output={"action": action, "reason": reason})
            if action == "reject":
                await self.repo.set_lead_progress(d.lead_id, status="rejected")
            else:
                with step_span("outbox", input={"draft_id": draft_id}) as sp:
                    wrote = await write_outbox(self.repo, draft_id, self.settings.outbox_path)
                    sp.update(output={"written": wrote})


_RUN_USAGE = (
    f"/run: process {DEFAULT_LIMIT} leads from the recorded export\n"
    "/run 20: process 20 leads\n"
    "/run <Sales Navigator search URL> 20: extract with Evaboot, then process 20"
)
_HELP = (
    "I send you drafts to approve, edit or reject. Commands:\n"
    f"{_RUN_USAGE}\n"
    "/learn: run a learning cycle now (it also runs every night)\n"
    "/status: what's waiting and what's running"
)


def _parse_run(args: list[str]) -> tuple[str | None, int] | None:
    url = next((a for a in args if a.startswith("http")), None)
    if url is not None and "linkedin.com/sales" not in url:
        return None
    rest = [a for a in args if a != url]
    if len(rest) > 1 or (rest and not rest[0].isdigit()):
        return None
    limit = int(rest[0]) if rest else DEFAULT_LIMIT
    return (url, limit) if 0 < limit <= 500 else None


def _main_buttons(draft_id: int) -> list[Button]:
    return [
        ("✅ Approve", f"a:{draft_id}"),
        ("✏️ Edit", f"e:{draft_id}"),
        ("🗑 Reject", f"r:{draft_id}"),
    ]


def _lead(row: object) -> Lead:
    return Lead(
        linkedin_url=getattr(row, "linkedin_url", ""),
        full_name=getattr(row, "name", ""),
        title=getattr(row, "title", ""),
        company=getattr(row, "company", ""),
    )
