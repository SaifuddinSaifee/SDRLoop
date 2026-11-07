"""Tool registry for the research agent: frozen toolset, typed results, budgeted, traced.

Tools are read-only. Everything they return is untrusted DATA and is wrapped in
<untrusted_content> before the model sees it.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from sdr_loop.budget import LeadBudget
from sdr_loop.llm import ToolCall, ToolSpec
from sdr_loop.models import Lead
from sdr_loop.observability import get_tracer

MAX_TEXT_CHARS = 6000
_CLOSE_TAG = re.compile(r"</?\s*untrusted_content[^>]*>", re.IGNORECASE)


@dataclass
class ToolResult:
    ok: bool
    text: str = ""  # cleaned page/feed text (what facts are validated against)
    source_url: str = ""
    error: str | None = None
    bytes_returned: int = 0
    # extra citable sources produced by one call (e.g. each news item): url -> text
    sources: dict[str, str] = field(default_factory=dict)


def wrap_untrusted(source: str, text: str) -> str:
    """Delimit tool output as data. A page cannot close the tag early: tag-like text is removed."""
    safe = _CLOSE_TAG.sub("", text)
    return f'<untrusted_content source="{source}">\n{safe}\n</untrusted_content>'


def render_for_model(result: ToolResult) -> str:
    if not result.ok:
        return f"ERROR: {result.error}"
    return wrap_untrusted(result.source_url, result.text)


async def _dns_public(host: str) -> bool:
    import asyncio

    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


@dataclass
class ToolContext:
    lead: Lead
    http: httpx.AsyncClient
    host_is_public: Callable[[str], Awaitable[bool]] = _dns_public
    timeout_s: float = 8.0


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict[str, Any]

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...


def site_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def host_allowed(url: str, company_website: str) -> bool:
    """fetch_page may only read the lead's own company site (and its subdomains)."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    allowed, host = site_host(company_website), site_host(url)
    return bool(allowed) and (host == allowed or host.endswith("." + allowed))


class ToolRegistry:
    """Frozen toolset. `execute` enforces the per-lead call budget and records a tool span."""

    def __init__(self, tools: list[Tool], budget: LeadBudget, ctx: ToolContext) -> None:
        self._tools = {t.name: t for t in tools}
        self._budget = budget
        self._ctx = ctx
        self.call_log: list[dict[str, Any]] = []

    @property
    def specs(self) -> list[ToolSpec]:
        return [ToolSpec(t.name, t.description, t.input_schema) for t in self._tools.values()]

    @property
    def exhausted(self) -> bool:
        return self._budget.tool_calls >= self._budget.max_tool_calls

    async def execute(self, call: ToolCall) -> tuple[ToolResult, bool]:
        """Returns (result, refused). A refused call (budget hit / unknown tool) runs nothing."""
        tool = self._tools.get(call.name)
        if tool is None:
            self.call_log.append({"tool": call.name, "refused": "unknown_tool"})
            return ToolResult(False, error=f"unknown tool {call.name}"), True
        if not self._budget.try_tool_call():
            self.call_log.append({"tool": call.name, "refused": "budget"})
            return ToolResult(False, error="tool call budget exhausted"), True
        with get_tracer().span(f"tool.{call.name}", kind="tool", input=call.input) as sp:
            try:
                result = await tool.run(call.input, self._ctx)
            except httpx.TimeoutException:
                result = ToolResult(False, error="timeout")
            except Exception as e:  # tools never raise into the agent loop (E5)
                result = ToolResult(False, error=f"{type(e).__name__}: {e}"[:200])
            sp.update(
                output={"ok": result.ok, "error": result.error, "bytes": result.bytes_returned},
                metadata={"url": result.source_url, "bytes": result.bytes_returned},
            )
        self.call_log.append(
            {
                "tool": call.name,
                "ok": result.ok,
                "error": result.error,
                "bytes": result.bytes_returned,
            }
        )
        return result, False
