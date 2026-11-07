# Plan: build evaboot-sdr-loop (PRD Must list, in phases)

## Context
The repo holds only the PRD (`docs/PRD Self-improving SDR agent (evaboot-sdr-loop).md`). The goal is a Python SDR agent for the Evaboot application. It turns a Sales Navigator search into qualified, researched, fact-cited two-email sequences, gets human approval on Telegram, and learns from edits and rejections through a champion vs challenger prompt loop. The whole run is traced in Langfuse and gated by evals in CI.

Decisions made with the user:
- Build the full **Must** list in phases that follow the PRD days. Each phase ends green. **Should** items come last.
- LLM keys: **Anthropic + Together AI** only. Three model families:
  - Claude writes.
  - Llama on Together qualifies.
  - Qwen or DeepSeek on Together is the critic (a different family from the writer).
- **Langfuse Cloud** (keys in `.env`).
- **No Evaboot access yet.** A synthetic CSV fixture follows Evaboot's export format. The real API client is built to the docs: `POST https://api.evaboot.com/v1/extractions/url/` with `linkedin_url`, `search_name` and `webhook_url`, Bearer auth, and a 202 response with `extraction_id`. Polling uses `GET /v1/extractions/{id}/`, which returns status `EXECUTED` and `prospects[]` keyed by columns like `"Full Name"`, `"Email"` and `"Email Status"` (`safe`, `riskier`, `suspect`, `invalid`), with `start`/`limit` paging.

## Repo setup and conventions
- `git init`, then commit at the end of each phase (this plan's approval authorises those commits).
- `uv` project, Python 3.12. Tooling: `ruff` (lint and format), `mypy --strict` on `src/`, and `pytest` with `pytest-asyncio`.
- `CLAUDE.md` goes at the **repo root**, so Claude Code loads it; `docs/` gets a pointer to it. Rules: tests first, no secrets in code or logs, every model call through `llm.py`, web text is data.
- `docs/decisions.md` and `docs/journal.md` get one line per phase.
- Config lives in `pydantic-settings` (`config.py`), read from env and `.env`. `.env.example` is committed. Flags: `DRY_RUN=true`, `LEARN_ENABLED=true`, `OFFLINE=false`, budget caps, `TELEGRAM_CHAT_ID`, and a model routing table.

## Layout (follows PRD; additions marked +)
```
src/sdr_loop/
  cli.py            # typer: run <url> | run --fixture | bot | api | learn | purge --lead | prompts push
  config.py      +  # Settings, model routing table, price table
  models.py      +  # Pydantic domain types: Lead, ICP, Qualification, Fact, Draft, Claim, CriticReport, Decision
  pipeline.py       # per-lead orchestration, asyncio.Semaphore(5), resume-by-step, one trace per lead
  llm.py            # THE gateway (see below)
  budget.py      +  # BudgetTracker (per-lead tokens/tool calls/USD, per-run USD) via contextvars
  observability.py+ # thin wrapper over Langfuse SDK + in-memory recorder for tests; PII mask fn
  prompts.py     +  # get champion/challenger from Langfuse prompt mgmt; fallback to prompts/*.md offline
  memory.py         # repository over SQLAlchemy 2.0 async
  db/schema.py   +  # ORM tables; alembic/ migrations
  steps/{extract,qualify,research,write,critic,approve,outbox}.py
  tools/{registry,fetch_page,search_news,read_lead}.py
  learn/{build_eval_set,challenger,promote}.py
  bot/telegram.py
  api/webhooks.py
prompts/            + # qualify.v1.md, writer.v1.md, critic.v1.md, research.v1.md (seed versions)
icp.yaml
fixtures/        +  # evaboot_export.csv (synthetic, ~300 leads), web/ (recorded HTML pages + news RSS, incl. injection page)
evals/              # golden_leads.jsonl, golden_facts.jsonl, test_qualify.py, test_write.py, conftest.py
tests/           +  # unit/integration tests T1–T22 with mocked LLM (FakeLLM) and SQLite
infra/              # Dockerfile, docker-compose.yml (postgres + app + bot + api), AWS task def (Should)
.github/workflows/ci.yml
```

## Core components (key design)

**`llm.py` gateway**. No other file may import `anthropic` or call Together.
- `async complete(task, messages, *, response_model=None, tools=None) -> LLMResult`.
- The task name (`qualify`, `research`, `write`, `critic`, `challenger`) is routed to a primary model and a fallback model through the config table.
- Providers:
  - `AsyncAnthropic` for Claude.
  - `httpx` against Together's OpenAI-compatible `/v1/chat/completions`.
- Timeout and retries come from `tenacity` (exponential backoff on 429, 5xx and timeouts), then the call falls back to the fallback model (E10).
- With `response_model`, the output is parsed with Pydantic. On a validation error there is one retry that includes the error text, then the fallback model (E9, T6).
- Before each call it checks the budget, and after each call it records tokens and cost from the price table. Over the cap it raises `BudgetExceeded` (E11, T20).
- Each call opens a Langfuse generation span with the model, tokens, cost and latency. Retries and fallbacks are recorded as span events.
- Default routing (editable in config). The actual Together model IDs get checked during the day-2 bakeoff:

  | Task | Primary | Fallback |
  | --- | --- | --- |
  | write | `claude-sonnet-5-5` | `claude-haiku-5-5` |
  | research (tool use) | `claude-haiku-5-5` | |
  | challenger | `claude-sonnet-5-5` | |
  | qualify | Llama-3.3-70B-Instruct-Turbo | Qwen |
  | critic | Qwen2.5-72B-Instruct-Turbo | DeepSeek-V3 (stays non-Claude) |

- Before coding the gateway, check current prices and SDK usage with the `claude-api` skill.

**Observability**
- One trace per lead, with tags for `run_id`, `prompt_versions` and `icp_version`.
- One span per step, seven in all. Tool calls are child spans with the URL and bytes returned.
- Scores go on the trace: ICP score, critic scores (four), code-check result, human decision, edit distance.
- `mask=` on the Langfuse client redacts emails and phone numbers.
- `trace_id` is stored on the lead so `purge` can delete the trace through the Langfuse public API.
- Tests use an `InMemoryTracer` with the same interface (T21 asserts one trace and seven spans).

**Data model** (PRD tables plus a few additions)
- PRD tables: `runs`, `leads` (unique `linkedin_url`), `qualifications`, `facts`, `drafts`, `decisions`, `eval_cases`, `prompt_promotions`.
- Additions:
  - `webhook_events` (unique `extraction_id`, for E2).
  - `runs.extraction_id`.
  - `leads.trace_id`, `leads.status`, `leads.skip_reason`.
  - `drafts.flagged` and `flag_reason`.
  - `decisions.uncited_human_claims` (E14).
- A crashed run resumes by checking which step rows already exist for each lead (E3, E18).
- Postgres runs through asyncpg in Compose, and SQLite through aiosqlite in tests.

**Steps**
1. **extract**
   - Fixture mode: `fixtures/evaboot_export.csv` is parsed through one column-alias map (`EVABOOT_COLUMNS`), so a real export can be dropped in later.
   - API mode: `EvabootClient.start_extraction`, then the FastAPI `POST /webhooks/evaboot/{secret}` endpoint receives the results. The secret is in the path, the `extraction_id` must belong to a known run, and processing is idempotent. A poll fallback runs after a timeout (E1).
   - Leads are upserted by LinkedIn URL. Leads whose email status isn't `safe` are skipped and the reason is stored (E4, T1–T3).
2. **qualify**
   - `icp.yaml` is loaded into an ICP model and hashed for `icp_version`.
   - Code rules run first: country, headcount range, title keywords, and the disqualifier company list. A rejection here makes no LLM call (T4, E12).
   - Then the LLM returns a `Qualification` (score 0 to 100, verdict, up to 3 reasons, any disqualifier hit).
   - Only fit leads continue. Maybe leads are logged.
3. **research**
   - A tool-use loop runs through the gateway. The registry is frozen: `fetch_page`, `search_news` (Google News RSS, no key needed) and `read_lead`.
   - `ToolBudget` allows at most 3 calls; a 4th is refused and the loop ends with the facts so far (T7). Each tool has a timeout and returns a typed error instead of raising (E5, T9).
   - Page text is extracted with selectolax and wrapped in `<untrusted_content>` delimiters. The system prompt marks that text as data.
   - **Facts are validated in code.** A snippet must be an exact substring (after whitespace normalisation) of content a tool really returned, or the fact is dropped. This handles injection and hallucination structurally (E6, T8).
   - Offline mode serves `fixtures/web/` through an httpx `MockTransport`.
4. **write**
   - Uses the champion prompt from Langfuse, or `prompts/writer.v1.md` offline.
   - Output is structured: `email_1` and `email_2`, each with a subject, body and `claims: [{text, fact_ids}]`.
   - With zero facts the writer produces a role-based email, and the draft is flagged (E7, T11). Non-English markets are flagged (E17).
5. **critic**
   - Code checks first, so a failure skips the LLM call (T12):
     - word limits (90 and 60)
     - a banned and spam word list
     - at most 1 link
     - exactly one ask (a heuristic)
     - every cited fact id exists for this lead
     - every claim has at least one fact id
   - Then the LLM critic scores faithfulness, personalisation, clarity and tone from 1 to 5, each with a reason, and lists any uncited personal claims (T13).
   - A failed draft goes back to the writer once with the notes. A second failure flags it for the human (E8).
6. **approve**
   - The draft is marked `pending_approval`. The bot process (`sdr-loop bot`) sends pending drafts with Approve, Edit and Reject buttons.
   - Edit: the operator replies to a ForceReply message with both emails separated by `---`.
   - Reject: a one-tap reason keyboard.
   - Only `TELEGRAM_CHAT_ID` gets an answer; every other chat is ignored and logged (T15).
   - A daily reminder goes out for pending drafts (E13). Edit distance is Levenshtein, via `rapidfuzz`.
7. **outbox**
   - Writes the approved sequence to `outbox.jsonl` only if an approval decision row exists (FR6.4, T14, T16).
   - No real-send adapters in v1.

**Learn loop** (`sdr-loop learn`, nightly)
1. `build_eval_set` turns each edit into an `eval_case` (input = lead and facts, expected = final text) and each rejection into a negative case.
2. If there are fewer than 10 new cases, the night is skipped and the reason is logged (E16). The job does nothing if `LEARN_ENABLED=false` or the prompt version is pinned.
3. `challenger` has Claude read the batch patterns and the current prompt and propose a revised writer prompt, which is saved to Langfuse with the label `challenger`.
4. `promote` runs champion and challenger over the eval cases and the fixed golden set, scoring both with code checks, the critic and edit distance. The challenger is promoted (the `production` label moves to it) only if all of these hold:
   - its average score is higher;
   - faithfulness doesn't drop;
   - **zero** invalid fact citations (hard gate);
   - it regresses on no more than 5% of cases.
5. A `prompt_promotions` row is written either way (T17–T19, E15).

**Evals** (`evals/`, `pytest -m eval`, real models)
- Golden set: about 50 leads with ICP labels, plus `golden_facts.jsonl`, frozen research facts so write evals stay deterministic.
- Qualify precision ≥ 0.85 and recall ≥ 0.80 (T5).
- Faithfulness = 100% (hard gate) (T10).
- Critic rubric average and code-check pass rate are reported.
- `evals/critic_agreement.py` compares the critic's scores with 20 human-scored drafts.

**CI** (`.github/workflows/ci.yml`)
- One job for ruff, mypy and unit tests (FakeLLM, SQLite, no network).
- One `evals` job that runs only when `ANTHROPIC_API_KEY` and `TOGETHER_API_KEY` secrets exist. It fails the PR on faithfulness below 100% or on missing precision and recall targets.

## Phases (each ends with tests green, a journal line and a commit)
1. **Foundations**
   - uv skeleton, ruff, mypy and pytest config, CI workflow.
   - `CLAUDE.md`, PRD moved to `docs/PRD.md`, `.env.example`.
   - config, models, DB schema and the Alembic initial migration, `memory.py`.
   - observability wrapper, budget, and the `llm.py` gateway with its FakeLLM provider.
   - Synthetic fixture CSV (about 300 leads mixing fit, not fit, no email and disqualified companies), `icp.yaml`, and a draft golden set.
2. **Extract and qualify:** Evaboot client, webhook API, dedup, code rules, LLM qualify. T1–T6.
3. **Research and write:** tool registry, budgets, offline web fixtures (including the injection page), fact validation, writer. T7–T11.
4. **Critic and evals:** code checks, LLM critic, rewrite-once loop, golden evals, critic agreement script. T12–T13.
5. **Approve and outbox:** Telegram bot and JSONL outbox, `pipeline.py` end to end with resume and concurrency. T14–T16, T20, T21.
6. **Learn loop and privacy:** eval cases, challenger, promote, `prompts push`, `purge --lead` with masking. T17–T19, T22.
7. **Ship (Must):**
   - Dockerfile and docker-compose (Postgres, app, api, bot).
   - README with the architecture diagram (Mermaid), setup steps and a KPI table template.
   - `docs/decisions.md`.
8. **Should list:** Streamlit review queue and KPI page, AWS ECS Fargate scheduled task, then decisions and journal polish.

## Things only you can do (I'll prompt at the right phase)
- Put your keys in `.env`: Anthropic, Together, Langfuse Cloud, and a Telegram bot token plus your chat id from BotFather.
- Hand-check the ICP labels in the draft golden set, since the PRD requires human labels.
- Score 20 drafts for the critic agreement check.
- Review about 50 drafts on Telegram for the first real learn round.
- Real numbers before and after a promotion can only come from your reviews, so the README KPI table stays a template until then.
- Later: Evaboot API key, and swap in a real export as the fixture.

## Verification
- `uv run ruff check . && uv run mypy src && uv run pytest` passes after every phase. These cover T1–T22 with mocked models and need no network.
- `uv run pytest -m eval` with real keys: qualify precision and recall plus a faithfulness of 100% on the golden set.
- End to end:
  1. `docker compose up -d postgres`
  2. `uv run alembic upgrade head`
  3. `uv run sdr-loop run --fixture --limit 10`
  4. Check the result: 10 traces in Langfuse with 7 spans and scores each, pending drafts in the DB, and the run report (skipped and qualified counts, cost per qualified lead, p95 latency).
- `uv run sdr-loop bot`: drafts arrive on your phone, Approve writes to `outbox.jsonl`, and a message from another chat is ignored.
- `uv run sdr-loop learn` with 10 or more seeded decisions: the challenger is created in Langfuse, the promotion decision is recorded in `prompt_promotions`, and the champion label moves only on a win.
- Definition of done (PRD): a fresh clone, then `docker compose up`, then `sdr-loop run --fixture`, shows traces and Telegram drafts within 10 minutes.
