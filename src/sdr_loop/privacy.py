"""`sdr-loop purge --lead`: delete a lead, its derived data, its outbox lines and its trace."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path

from sdr_loop.memory import Repository
from sdr_loop.observability import Tracer


@dataclass
class PurgeReport:
    found: bool
    rows: dict[str, int] = field(default_factory=dict)
    outbox_lines_removed: int = 0
    trace_deleted: bool = False


def _scrub_outbox(path: Path, linkedin_url: str) -> int:
    if not path.exists():
        return 0
    keep, removed = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and json.loads(line).get("lead", {}).get("linkedin_url") == linkedin_url:
            removed += 1
        elif line.strip():
            keep.append(line)
    path.write_text("".join(x + "\n" for x in keep), encoding="utf-8")
    return removed


async def purge_lead(
    repo: Repository, tracer: Tracer, ident: str | int, outbox_path: str | Path
) -> PurgeReport:
    lead = await repo.find_lead(ident)
    if lead is None:
        return PurgeReport(found=False)
    url, trace_id = lead.linkedin_url, lead.trace_id
    removed = await asyncio.to_thread(_scrub_outbox, Path(outbox_path), url)
    rows = await repo.purge_lead_rows(lead.id)
    if trace_id:
        tracer.delete_trace(trace_id)
        tracer.flush()
    return PurgeReport(True, rows, removed, bool(trace_id))
