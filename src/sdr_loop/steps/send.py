"""Step 8 (demo): deliver approved sequences to the local Mailpit inbox.

Approval schedules email 1 now and email 2 after FOLLOWUP_DELAY_MINUTES. A dispatcher (run by
`sdr-loop start`, or the "Send due emails now" button) sends whatever is due. Every send is a span
on the lead's trace. Nothing is scheduled unless an approval record exists and EMAIL_MODE=mailpit.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

from sdr_loop import mailer
from sdr_loop.campaign import load_campaign
from sdr_loop.config import Settings
from sdr_loop.memory import Repository
from sdr_loop.observability import get_tracer, lead_trace_seed, step_span
from sdr_loop.steps.outbox import final_sequence

log = logging.getLogger(__name__)
RETRY_AFTER = timedelta(minutes=1)
Sender = Callable[[Settings, EmailMessage], Awaitable[str]]


async def schedule_sequence(
    repo: Repository, draft_id: int, s: Settings, now: datetime | None = None
) -> int:
    """Schedule both emails of an approved draft. Returns how many were newly scheduled."""
    if s.email_mode != "mailpit":
        return 0
    final = await final_sequence(repo, draft_id)  # raises NotApproved without a decision
    draft = await repo.get_draft_by_id(draft_id)
    lead = await repo.get_lead(draft.lead_id)
    if not lead.email:
        return 0
    now = now or datetime.now(UTC)
    follow = now + timedelta(minutes=s.followup_delay_minutes)
    e1, e2 = final.email_1, final.email_2
    subject_2 = e2.subject or f"Re: {e1.subject}"
    n = 0
    for step, subject, body, due in (
        (1, e1.subject, e1.body, now),
        (2, subject_2, e2.body, follow),
    ):
        n += await repo.schedule_send(draft_id, draft.lead_id, step, lead.email, subject, body, due)
    return n


async def dispatch_due(
    repo: Repository, s: Settings, now: datetime | None = None, sender: Sender | None = None
) -> list[str]:
    """Send every due email. A follow-up waits until its email 1 has gone out."""
    send = sender or mailer.send
    campaign, _ = load_campaign()
    done: list[str] = []
    for row in await repo.due_sends(now or datetime.now(UTC)):
        parent = None
        if row.step == 2:
            first = await repo.get_send(row.draft_id, 1)
            if first is None or first.status != "sent":
                continue
            parent = first.message_id
        if not await repo.claim_send(row.id):
            continue  # another dispatcher took it
        msg = mailer.build_message(
            from_name=campaign.sender.name or campaign.company,
            from_email=campaign.sender.email or "sdr@sdr-loop.test",
            to=row.to_email, subject=row.subject, body=row.body, in_reply_to=parent,
        )  # fmt: skip
        with get_tracer().trace("lead", seed=lead_trace_seed(row.lead_id)):
            with step_span("send", input={"draft_id": row.draft_id, "email": row.step}) as sp:
                try:
                    message_id = await send(s, msg)
                except mailer.UnsafeDelivery as e:  # config problem: never retried blindly
                    await repo.finish_send(row.id, error=str(e))
                    sp.update(output={"sent": False, "error": "unsafe"})
                    continue
                except OSError as e:  # inbox down: try again in a minute
                    retry = datetime.now(UTC) + RETRY_AFTER
                    await repo.finish_send(row.id, error=f"{type(e).__name__}: {e}", retry_at=retry)
                    sp.update(output={"sent": False, "error": type(e).__name__})
                    log.warning(
                        "send %s: inbox unreachable (%s); retrying", row.id, type(e).__name__
                    )
                    continue
                await repo.finish_send(row.id, message_id=message_id)
                sp.update(output={"sent": True})
        done.append(f"draft {row.draft_id} email {row.step}")
    return done
