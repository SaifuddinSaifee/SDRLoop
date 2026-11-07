"""Step 1: extract. Parse Evaboot results (API payload or CSV fixture) into stored leads."""

from __future__ import annotations

import asyncio
import csv
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sdr_loop.evaboot import EvabootClient, EvabootError
from sdr_loop.memory import Repository
from sdr_loop.models import Lead

# canonical field -> accepted column names (case-insensitive). The ONE place to adapt when a real
# Evaboot export has different headers.
EVABOOT_COLUMNS: dict[str, list[str]] = {
    "full_name": ["Full Name", "Name"],
    "first_name": ["First Name"],
    "last_name": ["Last Name"],
    "title": ["Current Job", "Job Title", "Title"],
    "company": ["Company Name", "Company"],
    "company_website": ["Company Website", "Website", "Company Domain"],
    "industry": ["Industry", "Company Industry"],
    "headcount": ["Headcount", "Company Size", "Employees"],
    "country": ["Country", "Location Country"],
    "linkedin_url": ["LinkedIn Profile URL", "Profile URL", "LinkedIn URL", "input_url"],
    "email": ["Email", "Professional Email"],
    "email_status": ["Email Status", "Email Verification"],
}

VERIFIED = {"safe"}  # Evaboot statuses: safe | riskier | suspect | invalid


def normalise_linkedin_url(url: str) -> str:
    """Lowercase host+path, drop query/fragment/trailing slash so re-runs dedup (FR1.3)."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    return f"https://www.{host}{parts.path.rstrip('/')}" if host else url.strip().lower()


def _pick(record: dict[str, Any], field_name: str) -> str:
    lowered = {k.lower(): v for k, v in record.items()}
    for alias in EVABOOT_COLUMNS[field_name]:
        v = lowered.get(alias.lower())
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _headcount(raw: str) -> int | None:
    # Handles "25", "11-50 employees", "1,001-5,000" (takes the upper bound).
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", raw)]
    return max(nums) if nums else None


def record_to_lead(record: dict[str, Any]) -> Lead | None:
    url = _pick(record, "linkedin_url")
    if not url:
        return None
    name = (
        _pick(record, "full_name") or f"{_pick(record, 'first_name')} {_pick(record, 'last_name')}"
    )
    return Lead(
        linkedin_url=normalise_linkedin_url(url),
        full_name=name.strip(),
        title=_pick(record, "title"),
        company=_pick(record, "company"),
        company_website=_pick(record, "company_website"),
        industry=_pick(record, "industry"),
        headcount=_headcount(_pick(record, "headcount")),
        country=_pick(record, "country").upper(),
        email=_pick(record, "email").lower(),
        email_status=_pick(record, "email_status").lower(),
    )


def skip_reason(lead: Lead) -> str | None:
    """FR1.5 / E4: only leads with a verified email proceed."""
    if not lead.email:
        return "no_email"
    if lead.email_status not in VERIFIED:
        return f"email_not_verified:{lead.email_status or 'unknown'}"
    return None


def load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


@dataclass
class ExtractReport:
    total: int = 0
    new: int = 0
    duplicates: int = 0
    invalid: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    lead_ids: list[int] = field(default_factory=list)  # new + existing leads that proceed

    @property
    def processable(self) -> int:
        return len(self.lead_ids)


async def ingest(repo: Repository, run_id: int, records: list[dict[str, Any]]) -> ExtractReport:
    rep = ExtractReport(total=len(records))
    seen: set[str] = set()
    for rec in records:
        lead = record_to_lead(rec)
        if lead is None:
            rep.invalid += 1
            continue
        if lead.linkedin_url in seen:  # duplicate inside the same export
            rep.duplicates += 1
            continue
        seen.add(lead.linkedin_url)
        lead_id, created = await repo.upsert_lead(lead, run_id)
        if not created:
            rep.duplicates += 1
        else:
            rep.new += 1
        reason = skip_reason(lead)
        if reason:
            if created:
                await repo.set_lead_status(lead_id, "skipped", skip_reason=reason)
            rep.skipped[reason] += 1
            continue
        rep.lead_ids.append(lead_id)
    return rep


async def run_live_extraction(
    repo: Repository,
    client: EvabootClient,
    run_id: int,
    linkedin_url: str,
    webhook_url: str | None,
    *,
    webhook_wait_s: float = 120.0,
    poll_s: float = 5.0,
    poll_timeout_s: float = 900.0,
) -> None:
    """Start an Evaboot extraction and make sure the run ends 'extracted' or 'failed' (E1).

    The webhook (handled by the API process) normally fills the run. If it has not arrived after
    `webhook_wait_s` we poll the extraction status ourselves; if that also fails the run is
    marked failed with a clear reason. Ingest is idempotent, so a late webhook is harmless."""
    eid = await client.start_extraction(linkedin_url, f"sdr-loop run {run_id}", webhook_url)
    await repo.set_run_extraction(run_id, eid)
    waited = 0.0
    while webhook_url and waited < webhook_wait_s:
        run = await repo.get_run(run_id)
        if run is not None and run.status in ("extracted", "failed"):
            return
        await asyncio.sleep(poll_s)
        waited += poll_s
    try:
        prospects = await client.fetch_all(eid, poll_s=poll_s, timeout_s=poll_timeout_s)
    except EvabootError as e:
        await repo.set_run_status(run_id, "failed", reason=f"extraction not completed: {e}")
        return
    if await repo.claim_webhook(
        eid
    ):  # a webhook may have won the race; ingest is idempotent anyway
        await ingest(repo, run_id, prospects)
    await repo.set_run_status(run_id, "extracted")
