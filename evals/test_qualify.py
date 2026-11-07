"""T5: qualification precision/recall on the golden set (real models)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from sdr_loop.steps.extract import record_to_lead
from sdr_loop.steps.qualify import load_icp, qualify_lead

ROOT = Path(__file__).resolve().parents[1]
PRECISION_MIN, RECALL_MIN = 0.85, 0.80


def load_golden() -> list[dict[str, object]]:
    return [json.loads(x) for x in (ROOT / "evals/golden_leads.jsonl").read_text().splitlines()]


def prf(pairs: list[tuple[bool, bool]]) -> tuple[float, float]:
    tp = sum(1 for pred, gold in pairs if pred and gold)
    fp = sum(1 for pred, gold in pairs if pred and not gold)
    fn = sum(1 for pred, gold in pairs if not pred and gold)
    return (tp / (tp + fp) if tp + fp else 1.0), (tp / (tp + fn) if tp + fn else 1.0)


def test_prf_helper() -> None:
    assert prf([(True, True), (True, False), (False, True), (False, False)]) == (0.5, 0.5)


@pytest.mark.eval
async def test_t5_qualification_precision_recall_on_golden_set() -> None:
    icp, _ = load_icp(ROOT / "icp.yaml")
    sem = asyncio.Semaphore(5)

    async def one(row: dict[str, object]) -> tuple[bool, bool]:
        lead = record_to_lead(row["lead"])  # type: ignore[arg-type]
        assert lead is not None
        async with sem:
            q, _ = await qualify_lead(lead, icp)
        return q.verdict == "fit", row["label"] == "fit"

    pairs = await asyncio.gather(*(one(r) for r in load_golden()))
    precision, recall = prf(list(pairs))
    print(f"qualify precision={precision:.2%} recall={recall:.2%} n={len(pairs)}")
    assert precision >= PRECISION_MIN, f"precision {precision:.2%}"
    assert recall >= RECALL_MIN, f"recall {recall:.2%}"
