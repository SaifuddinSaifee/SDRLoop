"""Fresh start: delete leads, drafts, decisions, sent emails and (optionally) what the learning
loop has learned, the outbox file, the job history and the demo inbox.

Langfuse traces are not touched (delete them in Langfuse, or per lead with `sdr-loop purge`).
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

import httpx

from sdr_loop import mailer
from sdr_loop.config import Settings
from sdr_loop.jobs import JobStore
from sdr_loop.memory import Repository


class ResetRefused(Exception):
    pass


@dataclass(frozen=True)
class ResetPlan:
    data: bool = True  # leads, runs, drafts, decisions, sends
    learning: bool = True  # eval cases and promotion history
    outbox: bool = True
    jobs: bool = True
    inbox: bool = True


def _clear_files(s: Settings, store: JobStore, plan: ResetPlan) -> list[str]:
    done = []
    if plan.outbox and Path(s.outbox_path).exists():
        Path(s.outbox_path).write_text("")
        done.append("Emptied the outbox file.")
    if plan.jobs and store.dir.exists():
        shutil.rmtree(store.dir)
        done.append("Cleared the job history.")
    return done


async def fresh_start(s: Settings, plan: ResetPlan) -> list[str]:
    store = JobStore(s.state_dir)
    if (busy := store.current()) is not None:
        raise ResetRefused(f"{busy.label} is still running. Wait for it to finish.")
    done: list[str] = []
    if plan.data:
        repo = Repository(s.database_url)
        try:
            if s.database_url.startswith("sqlite"):
                await repo.init_schema()
            counts = await repo.reset_all(keep_learning=not plan.learning)
        finally:
            await repo.close()
        n = counts.get("leads", 0)
        done.append(f"Deleted {n} leads and everything derived from them.")
        if plan.learning:
            done.append("Cleared eval cases and promotion history.")
    done += await asyncio.to_thread(_clear_files, s, store, plan)
    if plan.inbox and s.email_mode == "mailpit":
        async with httpx.AsyncClient(timeout=10) as client:
            if await mailer.clear_inbox(s, client):
                done.append("Emptied the demo inbox.")
            else:
                done.append("Could not reach Mailpit to empty the demo inbox.")
    return done
