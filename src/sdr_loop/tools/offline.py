"""httpx MockTransport serving fixtures/web/ so tests and --fixture runs never hit the network."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

WEB = Path(__file__).resolve().parents[3] / "fixtures/web"


def _slug(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def offline_transport(web: Path = WEB) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        u = urlsplit(str(request.url))
        host = (u.hostname or "").removeprefix("www.")
        if host == "news.google.com":
            q = parse_qs(u.query).get("q", [""])[0].replace(" when:90d", "")
            f = web / "news" / f"{_slug(q)}.xml"
            if f.exists():
                return httpx.Response(
                    200, content=f.read_bytes(), headers={"content-type": "application/rss+xml"}
                )
            return httpx.Response(200, content=b'<rss version="2.0"><channel></channel></rss>')
        if host == "slow.example":
            raise httpx.ReadTimeout("fixture: site too slow", request=request)
        site = web / _slug(host.removesuffix(".example"))
        page = site / "index.html"
        if page.exists():
            return httpx.Response(
                200, content=page.read_bytes(), headers={"content-type": "text/html"}
            )
        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)
