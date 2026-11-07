# Decisions

1. **Fixed pipeline, one free agent (research).** Predictable cost, easy step-level tests.
2. **One LLM gateway (`llm.py`).** Retries, fallbacks, routing, budgets and spans in one place.
3. **Critic from a different family than the writer.** Claude writes; Qwen (Together) critiques; Llama (Together) qualifies.
4. **Facts validated in code.** A fact is kept only if its snippet is a substring of real tool output.
5. **Synthetic Evaboot fixture** until API access exists. One column-alias map to swap in a real export.
6. **"Verified email" means Evaboot status `safe` only.** `riskier`, `suspect`, `invalid` and empty are skipped with a stored reason (FR1.5).
7. **Webhook URL carries a secret and the run id** (`/webhooks/evaboot/{secret}/{run_id}`). Evaboot documents no signature header and an unspecified payload shape, so the run id, not the body, identifies the run; the extraction id is the idempotency key. If a body has `has_more`, only the first page is ingested by the webhook; the poll path pages fully.
8. **Resume by stage, not by step table.** `leads.stage` (qualified, researched, drafted) plus `leads.status` (outcome) is enough to resume; a budget stop leaves the stage untouched.
9. **One trace per lead, re-entered later.** The trace id is a hash of the lead id, so `approve` and `outbox` (hours later) append to the pipeline's trace. Cost: re-processing a lead in a second run appends to the same trace.
10. **Score decides the qualify verdict.** The model's verdict is overwritten from its score (fit ≥ 75, maybe ≥ 50) so thresholds are testable and consistent.
11. **Together/Qwen/Llama model ids are defaults, not a bakeoff result.** The PRD's day-2 bakeoff has not been run.
12. **Edit-distance and new-claim detection are heuristics.** Levenshtein on the canonical two-email text; E14 flags new numbers or non-sentence-initial capitalised words. Expect false positives and negatives.
13. **A challenger is scored with one writer attempt, no rewrite loop,** to measure prompt quality rather than the safety net.
14. **Evaluated eval cases are consumed.** After a cycle (win or lose) its cases are marked used; the next night needs 10 new ones.
15. **Offline runs skip the DNS "public host" check** because fixture domains are fake `.example` hosts. Live runs always apply it (found by an end-to-end run; covered by a regression test).
16. **httpx/telegram loggers are held at WARNING** because their INFO lines contain the Telegram bot token.
