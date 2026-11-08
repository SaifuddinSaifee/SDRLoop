"""Step 7: dry-run JSONL outbox. Nothing is written without an approval record (FR6.4)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sdr_loop.memory import Repository
from sdr_loop.models import Email, Sequence
from sdr_loop.steps.approve import parse_edit


class NotApproved(Exception):
    pass


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def final_sequence(repo: Repository, draft_id: int) -> Sequence:
    """The sequence as the human approved it (their edit, if any). Requires an approval record."""
    draft = await repo.get_draft_by_id(draft_id)
    decision = await repo.get_decision(draft_id)
    if decision is None or decision.action not in ("approve", "edit"):
        raise NotApproved(f"draft {draft_id} has no approval record")
    original = Sequence(email_1=Email(**draft.email_1), email_2=Email(**draft.email_2))
    return parse_edit(decision.final_text or "", original)


async def write_outbox(repo: Repository, draft_id: int, path: str | Path) -> bool:
    """Append the approved sequence to the JSONL outbox. Returns False if already written."""
    final = await final_sequence(repo, draft_id)
    draft = await repo.get_draft_by_id(draft_id)
    decision = await repo.get_decision(draft_id)
    assert decision is not None
    if draft.status == "outboxed":
        return False
    lead = await repo.get_lead(draft.lead_id)
    record = {
        "draft_id": draft_id,
        "decision": decision.action,
        "decided_at": decision.decided_at.isoformat(),
        "dry_run": True,
        "lead": {
            "name": lead.name, "title": lead.title, "company": lead.company,
            "email": lead.email, "linkedin_url": lead.linkedin_url,
        },
        "email_1": {"subject": final.email_1.subject, "body": final.email_1.body},
        "email_2": {"subject": final.email_2.subject, "body": final.email_2.body},
    }  # fmt: skip
    await asyncio.to_thread(_append_line, Path(path), json.dumps(record, ensure_ascii=False))
    await repo.set_draft_status(draft_id, "outboxed")
    await repo.set_lead_progress(draft.lead_id, status="outboxed")
    return True
