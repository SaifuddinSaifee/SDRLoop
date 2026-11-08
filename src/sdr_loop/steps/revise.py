"""Edit by instruction: the human says what to change ("shorter, lead with their 14-day launch"),
the writer revises its draft, and the critic checks the result exactly like a fresh draft (one
automatic retry, then flag). The human then approves, edits or rejects the new version.

Every revision is stored, so the learning loop compares the writer's ORIGINAL draft with what the
human finally approved, and sees the instructions that got it there.
"""

from __future__ import annotations

from sdr_loop.memory import Repository
from sdr_loop.models import Email, Sequence
from sdr_loop.observability import get_tracer, lead_trace_seed, step_span
from sdr_loop.steps.critic import review
from sdr_loop.steps.write import flags_for, write_sequence

MAX_REVISIONS = 3


class RevisionLimit(Exception):
    pass


def _seq(email_1: dict[str, object], email_2: dict[str, object]) -> Sequence:
    return Sequence(email_1=Email.model_validate(email_1), email_2=Email.model_validate(email_2))


async def revise(repo: Repository, draft_id: int, instruction: str) -> int:
    """Rewrite the draft per the instruction. Returns the revision number."""
    from sdr_loop.pipeline import lead_from_row

    if len(await repo.list_revisions(draft_id)) >= MAX_REVISIONS:
        raise RevisionLimit(
            f"Draft #{draft_id} was already rewritten {MAX_REVISIONS} times. Reply with the full "
            "text instead (email 1, a line with ---, email 2), or approve / reject it."
        )
    d = await repo.get_draft_by_id(draft_id)
    lead = lead_from_row(await repo.get_lead(d.lead_id))
    facts = await repo.get_facts(d.lead_id)
    current = _seq(d.email_1, d.email_2)
    human = f"The human reviewer asked for these changes: {instruction.strip()}"
    notes = human
    with get_tracer().trace("lead", seed=lead_trace_seed(d.lead_id)):
        with step_span("revise", input={"draft_id": draft_id, "instruction": instruction}) as sp:
            attempts = 0
            for _ in (1, 2):
                attempts += 1
                seq, ref = await write_sequence(lead, facts, revision_notes=notes, previous=current)
                report = await review(lead, facts, seq)
                if report.passed:
                    break
                notes = f"{human}\nAlso fix:\n{report.notes()}"
            flags = flags_for(lead, facts) + ([] if report.passed else ["critic_failed"])
            await repo.save_draft(
                d.lead_id, seq, prompt_version=d.prompt_version or ref,
                flagged=bool(flags), flag_reason=",".join(flags) or None,
                critic=report.model_dump(),
            )  # fmt: skip
            n = await repo.add_revision(
                draft_id, instruction.strip(), before=current.model_dump(), after=seq.model_dump()
            )
            sp.update(output={"revision": n, "passed": report.passed, "attempts": attempts})
    return n
