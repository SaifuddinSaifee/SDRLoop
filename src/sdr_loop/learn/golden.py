"""The fixed golden set, readable at runtime (the learn loop's second gate against overfitting)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sdr_loop.models import Fact, Lead
from sdr_loop.steps.extract import normalise_linkedin_url, record_to_lead

DEFAULT_DIR = Path("evals")


def _read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def golden_leads(directory: Path = DEFAULT_DIR) -> list[tuple[Lead, str]]:
    out = []
    for row in _read(directory / "golden_leads.jsonl"):
        lead = record_to_lead(row["lead"])
        assert lead is not None
        out.append((lead, row["label"]))
    return out


def golden_facts(directory: Path = DEFAULT_DIR) -> dict[str, list[Fact]]:
    return {
        normalise_linkedin_url(r["linkedin_url"]): [Fact(**f) for f in r["facts"]]
        for r in _read(directory / "golden_facts.jsonl")
    }


def golden_fit_with_facts(directory: Path = DEFAULT_DIR) -> list[tuple[Lead, list[Fact]]]:
    facts = golden_facts(directory)
    return [
        (lead, facts[lead.linkedin_url])
        for lead, label in golden_leads(directory)
        if label == "fit"
    ]
