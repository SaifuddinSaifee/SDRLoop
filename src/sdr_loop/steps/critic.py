"""Step 5: critic. Deterministic code checks first, then an LLM critic from another model family."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sdr_loop import llm
from sdr_loop.campaign import current_brief
from sdr_loop.models import (
    CodeCheckResult,
    CriticReport,
    CriticScores,
    Email,
    Fact,
    Lead,
    Sequence,
)
from sdr_loop.observability import step_span
from sdr_loop.prompts import PromptVersion, get_prompt
from sdr_loop.steps.write import flags_for, write_sequence

MAX_WORDS = {"email_1": 90, "email_2": 60}
MAX_LINKS = 1
MAX_SUBJECT_CHARS = 80
BANNED = (
    "free", "guarantee", "guaranteed", "act now", "limited time", "risk-free", "click here",
    "100%", "urgent", "no obligation", "winner", "congrats on", "congratulations on",
    "once in a lifetime", "buy now", "cash", "earn money", "dear sir", "dear madam",
)  # fmt: skip
_LINK = re.compile(r"https?://|www\.", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_CTA = re.compile(
    r"\b(let me know|reply|book|schedule|grab|hop on|open to|worth a|can we|could we|would you|"
    r"are you|do you|interested in|free for)\b",
    re.IGNORECASE,
)
FAITHFUL_MIN, AVG_MIN = 4, 3.5


def _words(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text))


def _banned_hits(text: str) -> list[str]:
    low = text.lower()
    return [b for b in BANNED if re.search(rf"(?<!\w){re.escape(b)}(?!\w)", low)]


def ask_count(body: str) -> int:
    """Sentences that ask for something: a question mark or an explicit call-to-action."""
    return sum(1 for s in _SENTENCE.split(body.strip()) if "?" in s or _CTA.search(s))


def code_checks(seq: Sequence, facts: list[Fact]) -> CodeCheckResult:
    known = {f.id for f in facts}
    fails: list[str] = []
    for name in ("email_1", "email_2"):
        e: Email = getattr(seq, name)
        n = _words(e.body)
        if n >= MAX_WORDS[name]:
            fails.append(f"{name}: {n} words, must be under {MAX_WORDS[name]}")
        if hits := _banned_hits(f"{e.subject} {e.body}"):
            fails.append(f"{name}: banned/spam words: {', '.join(hits)}")
        if len(_LINK.findall(e.body)) > MAX_LINKS:
            fails.append(f"{name}: more than {MAX_LINKS} link")
        if not e.subject.strip() or len(e.subject) > MAX_SUBJECT_CHARS:
            fails.append(f"{name}: subject missing or over {MAX_SUBJECT_CHARS} chars")
        if (a := ask_count(e.body)) != 1:
            fails.append(f"{name}: must contain exactly one ask, found {a}")
        for c in e.claims:
            if not c.fact_ids:
                fails.append(f"{name}: claim without a fact id: '{c.text[:60]}'")
            for fid in c.fact_ids:
                if fid not in known:
                    fails.append(f"{name}: cites fact id {fid} which does not exist for this lead")
    return CodeCheckResult(passed=not fails, failures=fails)


def faithfulness(seq: Sequence, facts: list[Fact]) -> tuple[int, int]:
    """(valid claims, total claims). Valid = at least one cited id and every id exists."""
    snippets = {f.id: f.snippet for f in facts if f.snippet.strip()}
    valid = total = 0
    for e in (seq.email_1, seq.email_2):
        for c in e.claims:
            total += 1
            if c.fact_ids and all(fid in snippets for fid in c.fact_ids):
                valid += 1
    return valid, total


async def llm_critic(
    lead: Lead, facts: list[Fact], seq: Sequence, prompt: PromptVersion | None = None
) -> CriticScores:
    prompt = prompt or get_prompt("critic")
    payload = {
        "ABOUT_US": current_brief(),
        "lead": {"name": lead.full_name, "title": lead.title, "company": lead.company},
        "facts": [{"id": f.id, "snippet": f.snippet} for f in facts],
        "draft": seq.model_dump(),
    }
    res = await llm.complete(
        "critic",
        prompt.text,
        [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        response_model=CriticScores,
        max_tokens=800,
    )
    scores: CriticScores = res.parsed
    if scores.uncited_claims and scores.faithfulness > 2:
        # Enforced in code, not trusted to the model: an uncited personal claim caps faithfulness.
        scores = scores.model_copy(update={"faithfulness": 2})
    return scores


async def review(lead: Lead, facts: list[Fact], seq: Sequence) -> CriticReport:
    code = code_checks(seq, facts)
    if not code.passed:  # T12: no LLM call when the cheap checks already fail
        return CriticReport(code=code)
    return CriticReport(code=code, scores=await llm_critic(lead, facts, seq))


@dataclass
class DraftOutcome:
    sequence: Sequence
    report: CriticReport
    prompt_ref: str
    attempts: int
    flags: list[str] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return bool(self.flags)


async def write_with_critic(
    lead: Lead, facts: list[Fact], writer_prompt: PromptVersion | None = None
) -> DraftOutcome:
    """Write, review, and on failure rewrite ONCE with the critic's notes (FR5.3, E8).
    A second failure is flagged for the human rather than retried forever."""
    flags = flags_for(lead, facts)
    notes: str | None = None
    for attempt in (1, 2):
        with step_span("write", metadata={"attempt": attempt}):
            seq, ref = await write_sequence(lead, facts, writer_prompt, revision_notes=notes)
        with step_span("critic", metadata={"attempt": attempt}) as sp:
            report = await review(lead, facts, seq)
            sp.update(output={"passed": report.passed, "code_failures": report.code.failures})
        if report.passed:
            return DraftOutcome(seq, report, ref, attempt, flags)
        notes = report.notes()
    flags.append("critic_failed")
    return DraftOutcome(seq, report, ref, 2, flags)
