from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser

from sdr_loop.tools.base import MAX_TEXT_CHARS, ToolContext, ToolResult, host_allowed

MAX_BYTES = 500_000
_WS = re.compile(r"\s+")


def html_to_text(html: str) -> str:
    tree = LexborHTMLParser(html)
    for sel in ("script", "style", "noscript", "nav", "footer", "header", "svg", "iframe"):
        for n in tree.css(sel):
            n.decompose()
    body = tree.body
    text = body.text(separator=" ") if body is not None else tree.text(separator=" ")
    return _WS.sub(" ", text).strip()


class FetchPage:
    name = "fetch_page"
    description = (
        "Fetch a page from the lead's own company website and return its text. "
        "Only the company's domain is allowed."
    )
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Absolute http(s) URL"}},
        "required": ["url"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not host_allowed(url, ctx.lead.company_website):
            return ToolResult(False, source_url=url, error="domain not allowed: company site only")
        host = urlsplit(url).hostname or ""
        if not await ctx.host_is_public(host):
            return ToolResult(False, source_url=url, error="host is not publicly routable")
        r = await ctx.http.get(url, timeout=ctx.timeout_s, follow_redirects=False)
        if r.status_code >= 400:
            return ToolResult(False, source_url=url, error=f"http {r.status_code}")
        if "html" not in r.headers.get("content-type", "text/html"):
            return ToolResult(False, source_url=url, error="not an html page")
        text = html_to_text(r.content[:MAX_BYTES].decode(r.encoding or "utf-8", errors="replace"))
        text = text[:MAX_TEXT_CHARS]
        if not text:
            return ToolResult(False, source_url=url, error="empty page")
        return ToolResult(True, text=text, source_url=url, bytes_returned=len(r.content))
