"""Approval bot logic, independent of the Telegram library so it is fully testable.

callback data:  a:<draft>  approve   e:<draft>  edit   r:<draft>  reject
                rr:<draft>:<reason>  reject with a one-tap reason
Only TELEGRAM_CHAT_ID is ever answered (T15); everyone else is ignored and logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sdr_loop.config import Settings
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
    def __init__(self, repo: Repository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings
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

    # ------------------------------------------------------------------ incoming

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
            await self._decide(draft_id, "approve")
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
            await self._decide(draft_id, "reject", reason=rest.split(":")[1])
            return Reply(f"🗑 Draft #{draft_id} rejected. Thanks, this feeds the learning loop.")
        log.warning("unknown callback %r", data)
        return None

    async def on_text(self, chat_id: int | str, text: str) -> Reply | None:
        if not self.authorised(chat_id):
            log.warning("ignored message from unauthorised chat %s", chat_id)
            return None
        draft_id = self._editing.get(str(chat_id))
        if draft_id is None:
            return Reply("Nothing to edit. Tap Edit under a draft first.")
        try:
            await self._decide(draft_id, "edit", new_text=text)
        except ap.EditFormatError as e:
            return Reply(f"Could not parse that: {e}", force_reply=True)
        del self._editing[str(chat_id)]
        return Reply(
            f"✏️ Draft #{draft_id} saved with your edit and written to the outbox (dry run)."
        )

    # ------------------------------------------------------------------ decisions

    async def _decide(
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
