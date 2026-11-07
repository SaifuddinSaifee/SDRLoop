from __future__ import annotations

from typing import Any

from sdr_loop.tools.base import ToolContext, ToolResult


class ReadLead:
    name = "read_lead"
    description = (
        "Read the lead's own fields from the Evaboot export "
        "(title, company, industry, size, country)."
    )
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        lead = ctx.lead
        title = lead.title or "unknown title"
        company = lead.company or "unknown company"
        text = (
            f"{lead.full_name} is {title} at {company}. "
            f"Industry: {lead.industry or 'unknown'}. "
            f"Company size: {lead.headcount or 'unknown'} employees. "
            f"Country: {lead.country or 'unknown'}."
        )
        return ToolResult(True, text=text, source_url=lead.linkedin_url, bytes_returned=len(text))
