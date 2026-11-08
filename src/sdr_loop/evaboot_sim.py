"""A local stand-in for the Evaboot API, for demos without a Sales Navigator seat or credits.

`EVABOOT_MODE=simulated` routes the real `EvabootClient` through this httpx transport. It speaks
the documented API: `POST /v1/extractions/url/` -> 202 + extraction_id;
`GET /v1/extractions/{id}/` -> 202 with progress while "extracting", then 200 EXECUTED with paged
`prospects` in Evaboot's export columns; `GET /v1/quota/`. Everything downstream (ingest, dedup,
email-status filtering, qualify, real web research, writing, approval, delivery) runs unchanged.

The prospects come from `fixtures/demo/sales_nav_demo.csv`: real companies, fictional people.
"""

from __future__ import annotations

import csv
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

DEMO_CSV = Path("fixtures/demo/sales_nav_demo.csv")
EXTRACT_SECONDS = 20.0


class EvabootSimulator:
    def __init__(
        self,
        csv_path: Path = DEMO_CSV,
        extract_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.csv_path = csv_path
        self.extract_seconds = EXTRACT_SECONDS if extract_seconds is None else extract_seconds
        self.clock = clock
        self.started: dict[str, float] = {}

    def prospects(self) -> list[dict[str, Any]]:
        with self.csv_path.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path.rstrip("/")
        if req.method == "GET" and path == "/v1/quota":
            return httpx.Response(200, json={"success": True, "quota": {
                "credits": 1000.0, "has_valid_salesnav": True, "simulated": True}})  # fmt: skip
        if req.method == "POST" and path == "/v1/extractions/url":
            body = json.loads(req.content or b"{}")
            if "linkedin.com/sales" not in str(body.get("linkedin_url", "")):
                return httpx.Response(
                    400, json={"error": "linkedin_url must be a Sales Navigator URL"}
                )
            eid = f"sim-{uuid.uuid4().hex[:12]}"
            self.started[eid] = self.clock()
            return httpx.Response(202, json={"extraction_id": eid, "status": "QUEUED"})
        if req.method == "GET" and path.startswith("/v1/extractions/"):
            eid = path.rsplit("/", 1)[-1]
            if eid not in self.started:
                return httpx.Response(404, json={"error": "unknown extraction"})
            elapsed = self.clock() - self.started[eid]
            if elapsed < self.extract_seconds:
                pct = int(100 * elapsed / self.extract_seconds)
                return httpx.Response(202, json={"status": "RUNNING", "progress": pct})
            rows = self.prospects()
            start = int(req.url.params.get("start", 0))
            limit = int(req.url.params.get("limit", 500))
            page = rows[start : start + limit]
            return httpx.Response(200, json={
                "status": "EXECUTED", "progress": 100, "total": len(rows),
                "prospects": page, "has_more": start + limit < len(rows)})  # fmt: skip
        return httpx.Response(404, json={"error": "not simulated"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)
