"""Evaboot API client (docs.evaboot.com): start an extraction, poll its status.

POST /v1/extractions/url/  {linkedin_url, search_name, webhook_url?} -> 202 {extraction_id, status}
GET  /v1/extractions/{id}/ -> 202 while running (progress); 200 when status == EXECUTED with
                              prospects[] (paged by start/limit, has_more).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

BASE_URL = "https://api.evaboot.com"
DONE = "EXECUTED"
FAILED = {"FAILED"}


class EvabootError(Exception):
    pass


class EvabootClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = BASE_URL,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._client = client or httpx.AsyncClient(base_url=base_url, timeout=timeout_s)
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def start_extraction(
        self, linkedin_url: str, search_name: str, webhook_url: str | None = None
    ) -> str:
        body: dict[str, Any] = {"linkedin_url": linkedin_url, "search_name": search_name[:255]}
        if webhook_url:
            body["webhook_url"] = webhook_url
        r = await self._client.post("/v1/extractions/url/", json=body, headers=self._headers)
        if r.status_code not in (200, 201, 202):
            raise EvabootError(f"start_extraction {r.status_code}: {r.text[:200]}")
        data = r.json()
        if not data.get("extraction_id"):
            raise EvabootError(f"no extraction_id in response: {data}")
        return str(data["extraction_id"])

    async def get_page(
        self, extraction_id: str, start: int = 0, limit: int = 500
    ) -> dict[str, Any]:
        r = await self._client.get(
            f"/v1/extractions/{extraction_id}/",
            params={"start": start, "limit": limit},
            headers=self._headers,
        )
        if r.status_code not in (200, 202):
            raise EvabootError(f"get_extraction {r.status_code}: {r.text[:200]}")
        data: dict[str, Any] = r.json()
        data["_http_status"] = r.status_code
        return data

    async def fetch_all(
        self, extraction_id: str, *, poll_s: float = 5.0, timeout_s: float = 900.0
    ) -> list[dict[str, Any]]:
        """Poll until EXECUTED (E1 fallback), then page through all prospects."""
        waited = 0.0
        while True:
            page = await self.get_page(extraction_id)
            status = page.get("status")
            if status in FAILED:
                raise EvabootError(f"extraction failed: {page.get('failure_reason')}")
            if page["_http_status"] == 200 and status == DONE:
                break
            if waited >= timeout_s:
                raise EvabootError(f"extraction {extraction_id} not finished after {timeout_s}s")
            await asyncio.sleep(poll_s)
            waited += poll_s
        prospects: list[dict[str, Any]] = list(page.get("prospects") or [])
        while page.get("has_more"):
            page = await self.get_page(extraction_id, start=len(prospects))
            prospects += page.get("prospects") or []
        return prospects
