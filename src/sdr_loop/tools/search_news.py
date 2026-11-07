from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

from defusedxml import ElementTree

from sdr_loop.tools.base import MAX_TEXT_CHARS, ToolContext, ToolResult

NEWS_URL = "https://news.google.com/rss/search?q={q}+when:90d&hl=en"
_WS = re.compile(r"\s+")


def parse_feed(xml: bytes, limit: int = 5) -> list[tuple[str, str]]:
    """Returns [(link, 'title (pubDate)')]. defusedxml blocks entity-expansion attacks."""
    root = ElementTree.fromstring(xml)
    items = []
    for it in root.iter("item"):
        title = _WS.sub(" ", (it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        date = (it.findtext("pubDate") or "").strip()
        if title and link:
            items.append((link, f"{title} ({date})" if date else title))
        if len(items) >= limit:
            break
    return items


class SearchNews:
    name = "search_news"
    description = "Search recent (last 90 days) news headlines about a company."
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"company": {"type": "string"}},
        "required": ["company"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        company = str(args.get("company") or ctx.lead.company).strip()
        url = NEWS_URL.format(q=quote_plus(company))
        r = await ctx.http.get(url, timeout=ctx.timeout_s, follow_redirects=False)
        if r.status_code >= 400:
            return ToolResult(False, source_url=url, error=f"http {r.status_code}")
        items = parse_feed(r.content)
        if not items:
            return ToolResult(
                True, text="No recent news found.", source_url=url, bytes_returned=len(r.content)
            )
        text = "\n".join(f"- {t} <{link}>" for link, t in items)[:MAX_TEXT_CHARS]
        # each headline is individually citable under its own link
        return ToolResult(
            True,
            text=text,
            source_url=url,
            bytes_returned=len(r.content),
            sources={link: t for link, t in items},
        )
