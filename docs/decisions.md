# Decisions

1. **Fixed pipeline, one free agent (research).** Predictable cost, easy step-level tests.
2. **One LLM gateway (`llm.py`).** Retries, fallbacks, routing, budgets and spans in one place.
3. **Critic from a different family than the writer.** GLM-5.3 writes; DeepSeek V4.1 Flash critiques. A model grading its own family's style is too lenient.
4. **Facts validated in code.** A fact is kept only if its snippet is a substring of real tool output.
5. **Synthetic Evaboot fixture** until API access exists. One column-alias map to swap in a real export.
6. **"Verified email" means Evaboot status `safe` only.** `riskier`, `suspect`, `invalid` and empty are skipped with a stored reason (FR1.5).
7. **Webhook URL carries a secret and the run id** (`/webhooks/evaboot/{secret}/{run_id}`). Evaboot documents no signature header and an unspecified payload shape, so the run id, not the body, identifies the run; the extraction id is the idempotency key. If a body has `has_more`, only the first page is ingested by the webhook; the poll path pages fully.
8. **Resume by stage, not by step table.** `leads.stage` (qualified, researched, drafted) plus `leads.status` (outcome) is enough to resume; a budget stop leaves the stage untouched.
9. **One trace per lead, re-entered later.** The trace id is a hash of the lead id, so `approve` and `outbox` (hours later) append to the pipeline's trace. Cost: re-processing a lead in a second run appends to the same trace.
10. **Score decides the qualify verdict.** The model's verdict is overwritten from its score (fit ≥ 75, maybe ≥ 50) so thresholds are testable and consistent.
11. **Model ids are from Together's serverless table, not a bakeoff result.** The PRD's day-2 bakeoff has not been run, and the ids have not been called live.
12. **Edit-distance and new-claim detection are heuristics.** Levenshtein on the canonical two-email text; E14 flags new numbers or non-sentence-initial capitalised words. Expect false positives and negatives.
13. **A challenger is scored with one writer attempt, no rewrite loop,** to measure prompt quality rather than the safety net.
14. **Evaluated eval cases are consumed.** After a cycle (win or lose) its cases are marked used; the next night needs 10 new ones.
15. **Offline runs skip the DNS "public host" check** because fixture domains are fake `.example` hosts. Live runs always apply it (found by an end-to-end run; covered by a regression test).
16. **httpx/telegram loggers are held at WARNING** because their INFO lines contain the Telegram bot token.
17. **Together AI only (2026-11-07).** No Claude or Anthropic SDK anywhere. write and challenger use `zai-org/GLM-5.3`; research (tool use) and qualify use `zai-org/GLM-5.3-Flash`; critic uses `deepseek-ai/DeepSeek-V4.1-Flash`. Fallbacks: GLM-5.3 Flash for write, GLM-5.3 for research, DeepSeek V4.1 Flash for qualify, DeepSeek V4 Pro for critic.
18. **The Llama 3.3 / Qwen 2.5 / DeepSeek V3 ids from the first draft were dropped:** Together's serverless table does not list them. The qualifier is now GLM Flash (no independence requirement there); only the critic must differ from the writer's family.
19. **GLM-5.3 quirks handled in the provider, not the callers.** It always thinks (cannot be disabled) and the trace shares `max_tokens`, so the provider adds headroom (1024 at `low`) and sends `reasoning_effort` (default `low`, `GLM_REASONING_EFFORT`). It sends Together's recommended `temperature=1.0, top_p=0.95` for GLM and 0.0 for other models, and reads only `content` (never `reasoning_content`; inline `<think>` blocks are stripped). Higher sampling temperature means more run-to-run variance in evals than the earlier temperature-0 setup.
20. **Tests keep a two-sided fake** ("writer": write/research/challenger, "judge": qualify/critic) on the real route table's model ids, via `routes_for_fakes()`, so prices and routing stay in sync with `config.py`.
21. **One command, one control panel (2026-11-08).** `sdr-loop start` checks and fixes setup, then runs the Streamlit control panel, the Telegram bot and the nightly learn schedule together. Runs and learning cycles are detached background jobs (`jobs.py`: one CLI process each, a JSON record plus a log under `STATE_DIR`), started from the panel, Telegram `/run`, or the schedule. They are processes rather than threads so a page reload or a bot restart never kills a run, and the CLI stays the single code path. One job at a time: two runs would share the run budget and the leads. Streamlit became a core dependency because the panel is now the main interface.
