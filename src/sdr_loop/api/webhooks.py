"""FastAPI app receiving Evaboot webhooks (verified secret, processed once)."""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from sdr_loop.config import Settings
from sdr_loop.memory import Repository
from sdr_loop.steps.extract import ExtractReport, ingest

log = logging.getLogger(__name__)

OnExtracted = Callable[[int, ExtractReport], Awaitable[None]]


def create_app(
    repo: Repository, settings: Settings, on_extracted: OnExtracted | None = None
) -> FastAPI:
    app = FastAPI(title="sdr-loop webhooks")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/evaboot/{secret}/{run_id}")
    async def evaboot(secret: str, run_id: int, request: Request) -> dict[str, Any]:
        # The URL we register carries a secret; Evaboot documents no signature header.
        if not hmac.compare_digest(secret, settings.evaboot_webhook_secret):
            log.warning("webhook with bad secret rejected")
            raise HTTPException(status_code=404)
        run = await repo.get_run(run_id)
        if run is None or not run.extraction_id:
            raise HTTPException(status_code=404, detail="unknown run")
        body: dict[str, Any] = await request.json()
        if body.get("extraction_id") and str(body["extraction_id"]) != run.extraction_id:
            raise HTTPException(status_code=400, detail="extraction id mismatch")
        if not await repo.claim_webhook(run.extraction_id):  # E2: same delivery twice
            return {"status": "duplicate"}
        if body.get("status") == "FAILED":
            await repo.set_run_status(
                run_id, "failed", reason=f"evaboot: {body.get('failure_reason') or 'failed'}"
            )
            return {"status": "failed"}
        report = await ingest(repo, run_id, list(body.get("prospects") or []))
        await repo.set_run_status(run_id, "extracted")
        if on_extracted is not None:
            await on_extracted(run_id, report)
        return {"status": "ok", "new": report.new, "skipped": sum(report.skipped.values())}

    return app
