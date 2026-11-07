from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sdr_loop.models import Fact, Lead
from sdr_loop.steps.extract import normalise_linkedin_url, record_to_lead

EVALS = Path(__file__).resolve().parent


def _read(name: str) -> list[dict[str, Any]]:
    return [json.loads(x) for x in (EVALS / name).read_text().splitlines() if x.strip()]


def golden_leads() -> list[tuple[Lead, str]]:
    """(lead, label) with label in {fit, not_fit}."""
    out = []
    for row in _read("golden_leads.jsonl"):
        lead = record_to_lead(row["lead"])
        assert lead is not None
        out.append((lead, row["label"]))
    return out


def golden_facts() -> dict[str, list[Fact]]:
    return {
        normalise_linkedin_url(r["linkedin_url"]): [Fact(**f) for f in r["facts"]]
        for r in _read("golden_facts.jsonl")
    }


def golden_fit_with_facts() -> list[tuple[Lead, list[Fact]]]:
    facts = golden_facts()
    return [(lead, facts[lead.linkedin_url]) for lead, label in golden_leads() if label == "fit"]
