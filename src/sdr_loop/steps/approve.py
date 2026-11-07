"""Step 6: human approval logic (Telegram is only the transport; see bot/telegram.py)."""

from __future__ import annotations

import re
from typing import Any

from rapidfuzz.distance import Levenshtein

from sdr_loop.memory import Repository
from sdr_loop.models import Claim, Email, Fact, Lead, Sequence

REJECT_REASONS = ("wrong_fit", "wrong_fact", "bad_tone", "too_long", "other")
SEPARATOR = "\n---\n"
_SUBJECT = re.compile(r"^\s*subject:\s*(.+)$", re.IGNORECASE)
_SUBJECT_PREFIX = re.compile(r"^\s*subject:\s*", re.IGNORECASE)


class EditFormatError(ValueError):
    pass


def email_text(e: Email | dict[str, Any]) -> str:
    subject, body = (e.subject, e.body) if isinstance(e, Email) else (e["subject"], e["body"])
    return f"Subject: {subject}\n\n{body}"


def sequence_text(seq: Sequence) -> str:
    """Canonical plain-text form: used for Telegram edits, edit distance and eval cases."""
    return email_text(seq.email_1) + SEPARATOR + email_text(seq.email_2)


def parse_edit(text: str, original: Sequence) -> Sequence:
    """Parse '<email 1> --- <email 2>' (each with an optional 'Subject:' first line).
    Claims are dropped for edited emails: the human owns the text, nothing in it is 'grounded'."""
    parts = re.split(r"^\s*---\s*$", text.strip(), flags=re.MULTILINE)
    if len(parts) != 2 or not all(p.strip() for p in parts):
        raise EditFormatError("Send both emails separated by a line containing only ---")
    emails = []
    for part, fallback in zip(parts, (original.email_1, original.email_2), strict=True):
        lines = part.strip().splitlines()
        m = _SUBJECT.match(lines[0]) if lines else None
        subject = m.group(1).strip() if m else fallback.subject
        body = "\n".join(lines[1:] if m else lines).strip()
        if not body:
            raise EditFormatError("An email body is empty")
        emails.append(Email(subject=subject, body=body, claims=[]))
    return Sequence(email_1=emails[0], email_2=emails[1])


def edit_distance(original: str, final: str) -> int:
    return int(Levenshtein.distance(original, final))


_NUMBER = re.compile(r"\d[\d,.%]*")
_PROPER = re.compile(r"[A-Z][a-z]{2,}")


def _proper_nouns(text: str) -> list[str]:
    """Capitalised words that are not sentence-initial (so 'Reply?' or 'Follow up' don't count)."""
    out = []
    for sent in re.split(r"[.!?\n]+", text):
        sent = _SUBJECT_PREFIX.sub("", sent)
        for w in sent.split()[1:]:
            w = w.strip(",;:()\"'")
            if _PROPER.fullmatch(w):
                out.append(w)
    return out


def introduces_new_claims(original: str, final: str, facts: list[Fact]) -> bool:
    """E14 heuristic: the human added numbers or proper nouns that are in neither the draft nor the
    facts. Allowed, but recorded so the learn step never treats them as grounded."""
    known = (original + " " + " ".join(f.snippet for f in facts)).lower()
    new_numbers = [n for n in _NUMBER.findall(final) if n.lower().strip(".,") not in known]
    new_names = [w for w in _proper_nouns(final) if w.lower() not in known]
    return bool(new_numbers or new_names)


async def approve(repo: Repository, draft_id: int) -> int:
    d = await repo.get_draft_by_id(draft_id)
    seq = Sequence(email_1=Email(**d.email_1), email_2=Email(**d.email_2))
    return await repo.record_decision(
        draft_id, "approve", final_text=sequence_text(seq), edit_distance=0
    )


async def edit(repo: Repository, draft_id: int, new_text: str, facts: list[Fact]) -> int:
    d = await repo.get_draft_by_id(draft_id)
    original = Sequence(email_1=Email(**d.email_1), email_2=Email(**d.email_2))
    final = sequence_text(parse_edit(new_text, original))  # raises EditFormatError
    before = sequence_text(original)
    return await repo.record_decision(
        draft_id,
        "edit",
        final_text=final,
        edit_distance=edit_distance(before, final),
        uncited_human_claims=introduces_new_claims(before, final, facts),
    )


async def reject(repo: Repository, draft_id: int, reason: str) -> int:
    if reason not in REJECT_REASONS:
        raise ValueError(f"reason must be one of {REJECT_REASONS}")
    return await repo.record_decision(draft_id, "reject", reason=reason)


def review_text(lead: Lead, d: Any, facts: list[Fact]) -> str:
    """Telegram message body for one draft."""
    flags = f"\n⚠️ Flagged: {d.flag_reason}" if d.flagged else ""
    cs = d.critic_scores or {}
    s = cs.get("scores") or {}
    score = (
        f"\nCritic: faith {s['faithfulness']} · pers {s['personalisation']} · "
        f"clarity {s['clarity']} · tone {s['tone']}"
        if s
        else ""
    )
    cited = {
        fid for e in (d.email_1, d.email_2) for c in e.get("claims", []) for fid in c["fact_ids"]
    }
    src = "\n".join(f"[{f.id}] {f.snippet}" for f in facts if f.id in cited) or "(no cited facts)"
    return (
        f"Draft #{d.id} · {lead.full_name}, {lead.title} @ {lead.company}{flags}{score}\n\n"
        f"{email_text(d.email_1)}\n\n— follow-up —\n\n{email_text(d.email_2)}\n\nSources:\n{src}"
    )[:4000]


__all__ = [
    "REJECT_REASONS",
    "Claim",
    "EditFormatError",
    "approve",
    "edit",
    "edit_distance",
    "introduces_new_claims",
    "parse_edit",
    "reject",
    "review_text",
    "sequence_text",
]
