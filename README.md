# evaboot-sdr-loop

A self-improving SDR agent. It takes a LinkedIn Sales Navigator search, gets clean leads through the
[Evaboot API](https://docs.evaboot.com), qualifies them against an ICP, researches each fit lead,
writes a two-email sequence where **every personal claim cites a fact the agent actually read**, and
sends it to a human for approval on Telegram. Every edit or rejection becomes a test case. The agent
changes its own writer prompt only when a challenger version beats the champion on those tests, and it
can never promote a version that cites a fact that does not exist.

Dry run by default: nothing is ever sent. Full spec: [`docs/PRD.md`](docs/PRD.md).

## Status: read this first

| | |
|---|---|
| Built and tested | Whole pipeline, approval flow, learn loop, purge, KPI queries, Streamlit pages. 139 tests; the suite also passes against Postgres. |
| **Not yet run with real services** | The Together AI provider and its GLM models, Langfuse, the Telegram bot against Telegram, the Evaboot API. Tests use scripted fake models and recorded fixtures; the first real run may surface integration bugs. |
| **No quality numbers yet** | The KPI table below is a template. Precision, recall, faithfulness on real models, and before/after promotion numbers need real keys and ~50 human reviews. |
| Not built | AWS deployment, Instantly/Gmail adapters, simulated-reply feed, qualify-prompt learning, Langfuse datasets, Evaboot MCP. |
| Fixture is synthetic | `fixtures/evaboot_export.csv` is generated (`scripts/make_fixture.py`) to look like an Evaboot export. Only a few column names are documented by Evaboot; map a real export in `EVABOOT_COLUMNS` ([`steps/extract.py`](src/sdr_loop/steps/extract.py)). |
| Golden labels are drafts | `evals/golden_leads.jsonl` is labelled by archetype, not by hand. Review it before trusting precision/recall. |

## How it works

<p align="center">
  <img src="pipeline-animated.svg" alt="Animated pipeline: Sales Nav URL → extract → qualify → research → write → critic → human approve → outbox.jsonl, with edits and rejections feeding a nightly learning loop that promotes a better writer prompt" width="100%">
</p>

A fixed Python pipeline; only research is a free agent. One Langfuse trace per lead, one span per step,
scores on the trace. The details that matter:

- **Grounding is checked in code.** Research facts are kept only if the snippet is a verbatim substring
  of text a tool really returned for that URL. The writer must cite fact ids; the critic's code checks
  fail any claim with a missing or non-existent id; the promotion gate rejects a challenger with even one.
- **Web text is data.** Tool output is wrapped in `<untrusted_content>` (the wrapper cannot be closed from
  inside), the toolset is frozen and read-only, `fetch_page` can only read the lead's own company domain
  (and refuses non-public hosts), and instruction-like snippets are dropped. Tested with a scripted model
  that *obeys* an injected page; whether a real model resists is an eval question, not proven here.
- **The critic is a different model family** from the writer. Everything runs on Together AI: GLM-5.3
  writes (GLM-5.3 Flash researches and qualifies) and DeepSeek V4.1 Flash critiques. An uncited claim
  caps faithfulness at 2 in code, not by trusting the model.
- **Budgets stop runs cleanly.** Per-lead (USD, tokens, 3 tool calls) and per-run (USD). A stopped lead
  never produces a partial draft. Runs resume by stage after a crash.
- **Nothing leaves without a human.** The outbox refuses any draft without an approval record. The bot
  answers only `TELEGRAM_CHAT_ID`. `DRY_RUN=false` refuses to start (v1 has no sending adapters).

## Architecture

How the app is built, as opposed to how a lead flows through it (the diagram above).

**Diagram: [`docs/architecture.excalidraw`](docs/architecture.excalidraw)** (editable source). Open it by
dragging the file onto [excalidraw.com](https://excalidraw.com), or with the VS Code *Excalidraw* extension.
It shows five layers: entry points (CLI, FastAPI webhook, Telegram bot, Streamlit); the application
(pipeline orchestrator and its seven steps, the read-only research tools, the learning loop); the platform
(the single LLM gateway, Langfuse tracing, the repository, prompt management); the external services and
storage behind them (Together AI, Langfuse Cloud, PostgreSQL, local files); and the stack.
Blue marks the model gateway, red the untrusted-input boundary, purple the learning loop.

| Layer | Choice |
|---|---|
| Language and runtime | Python 3.12, [uv](https://docs.astral.sh/uv/), `asyncio` (5 leads in flight) |
| Data and validation | Pydantic v2 (domain types, settings, and the schemas model output must satisfy), SQLAlchemy 2.0 async, Alembic, PostgreSQL 16 (SQLite for tests and local mode) |
| HTTP | FastAPI + uvicorn for the Evaboot webhook; `httpx` for Evaboot, Together AI and the research tools; `tenacity` for retries |
| Models | Together AI only, over its OpenAI-compatible API, behind one gateway: GLM-5.3 (write, challenger), GLM-5.3 Flash (research, qualify), DeepSeek V4.1 Flash (critic) |
| Observability and prompts | Langfuse: traces, scores, and prompt versions with `production` / `challenger` labels |
| Approval and UI | `python-telegram-bot` (long polling), Streamlit (review queue and KPI page) |
| Parsing and safety | `selectolax` (HTML to text), `defusedxml` (news RSS), `rapidfuzz` (edit distance) |
| Quality | `pytest` + `pytest-asyncio`, `ruff`, `mypy --strict` on `src/`, GitHub Actions |
| Packaging | Docker, Docker Compose (Postgres, migrations, API, bot, CLI) |

The structure enforces a few rules, each with exactly one home:

- **One door to models.** Only `llm.py` talks to a provider. Routing, retries, the fallback model, JSON-schema validation with a corrective retry, budget checks, cost accounting and the Langfuse generation span all live there, so no step can skip them.
- **A fixed pipeline with one free agent.** `pipeline.py` orchestrates plain-Python steps; only research chooses its own actions, and its toolset is frozen, read-only and budgeted. Steps are idempotent by lead, so a crashed or budget-stopped run resumes by stage.
- **One door to the database.** `memory.py` is the only module that issues queries; everything else works with typed objects.
- **Telegram is transport, not logic.** The approval rules live in `BotController`, which the Streamlit page reuses, so a decision made on the web gets the same guards, tracing and outbox behaviour as one made on a phone.
- **Tests never touch a paid service.** `FakeProvider`, an offline HTTP transport serving recorded pages, and a SQLite database stand in for every dependency. The same suite also runs against Postgres.

Why these choices were made is in [`docs/decisions.md`](docs/decisions.md).

## Quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env      # add the keys you have; everything else is optional
uv run sdr-loop start
```

`sdr-loop start` is the only command you need day to day. It:

1. **Checks your setup and fixes what it can.** It creates the local database, saves your Telegram
   chat id as soon as you message the bot, and uploads any missing prompts to Langfuse. Anything it
   can't fix is listed with the exact fix.
2. **Opens the control panel** at http://localhost:8501:
   - **Home:** start a run, watch its progress live, and see setup status.
   - **Review queue:** each draft next to its cited sources, with Approve / Edit / Reject.
   - **Learning:** run a learning cycle and see the promotion history.
   - **KPIs.**
3. **Runs the Telegram bot.** Drafts arrive on your phone within seconds of being written, and you
   get a message when each run finishes. From the phone you can also send `/run 20`,
   `/run <Sales Navigator URL> 20`, `/status` and `/learn`.
4. **Runs the learning cycle every night** at `LEARN_AT` (default 02:00 local time).

Ctrl-C stops it. A run that is already going finishes on its own, and a crashed run can be resumed.

What each key unlocks (the Home page shows which ones are working):

| `.env` | Without it | With it |
|---|---|---|
| `TOGETHER_API_KEY` | demo runs only (canned AI output, tick "Demo mode") | real qualify / research / write / critic |
| `TELEGRAM_BOT_TOKEN` | review in the control panel | review on your phone too |
| `LANGFUSE_*` | no traces | one trace per lead, scores, prompt versions |
| `EVABOOT_API_KEY` | runs on the recorded export only | runs on a live Sales Navigator search |

Telegram setup: in Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, pick a
name and a username ending in `bot`, and put the token in `.env` as `TELEGRAM_BOT_TOKEN`. Then run
`sdr-loop start` and press **Start** in your bot when it asks. The chat id is saved for you. The
bot only ever answers that one chat. If a token leaks, send `/revoke` to @BotFather.

Docker (Postgres + migrations + control panel + bot + webhook API):

```bash
docker compose up -d --build     # control panel on http://localhost:8501
```

Postgres is published on `127.0.0.1:55432` for dev tools; the API on `${API_PORT:-8000}`.

The individual commands below are what `start` and the control panel use. They're useful in
scripts, Docker and debugging:

| Command | What it does |
|---|---|
| `sdr-loop start` | everything above |
| `sdr-loop check` | setup checks and automatic fixes only (exit 1 if something is broken) |
| `sdr-loop run [URL] [--fixture] [--fake-llm] [--limit N] [--resume RUN_ID]` | extract → qualify → research → write → critic |
| `sdr-loop bot` | only the Telegram bot |
| `sdr-loop telegram` | check the bot token, find your chat id, send a test message |
| `sdr-loop api` | Evaboot webhook receiver (`POST /webhooks/evaboot/{secret}/{run_id}`) |
| `sdr-loop learn` | one learning cycle (needs ≥ 10 new edits/rejections) |
| `sdr-loop purge --lead <id or LinkedIn URL>` | delete the lead, derived data, outbox lines and trace |
| `sdr-loop prompts push` | upload the local prompt files to Langfuse as new `production` versions |

Configuration is environment variables (see `.env.example`): budgets (`PER_LEAD_USD_CAP`, `PER_RUN_USD_CAP`,
`MAX_TOOL_CALLS`), `CONCURRENCY`, `LEARN_ENABLED`, `LEARN_AT`, `PINNED_PROMPTS`, `OFFLINE`. The ICP lives in
[`icp.yaml`](icp.yaml); reuse the agent for another product by editing that file.

## Observability

Per lead: a trace tagged with run id and ICP version, a span per step (`extract`, `qualify`, `research`,
`write`, `critic`, `approve`, `outbox`) carrying rolled-up tokens and cost, generation children with model
and latency, tool children with the URL and bytes returned, and retries/fallbacks as span events.
Scores: `icp_score`, `faithfulness`, `personalisation`, `clarity`, `tone`, `code_check`, `human_decision`,
`edit_distance`. Emails and phone numbers are masked before export. `approve`/`outbox` happen hours later
and are appended to the same trace.

## Evals and CI

```bash
uv run pytest                  # 139 tests: mocked models, CSV fixtures, no network
uv run pytest -m eval          # real models on the golden set (needs TOGETHER_API_KEY)
TEST_DATABASE_URL=postgresql+asyncpg://sdr:sdr@127.0.0.1:55432/sdr uv run pytest   # same suite on Postgres
uv run python -m evals.critic_agreement export   # then fill in your scores, then `compare`
```

Gates: qualify precision ≥ 85% and recall ≥ 80%; **faithfulness 100%** (hard); writing quality via code
checks and critic average. The critic-agreement tool tells you whether the critic can be trusted to
promote prompts; run it before relying on the learn loop. CI (`.github/workflows/ci.yml`) runs lint, format,
mypy strict, and the unit suite on every PR; the eval job runs only when API secrets exist.

## The learning loop

Edits and rejections become eval cases (`sdr-loop learn` builds them, nightly in production). With at
least 10 new cases, the model proposes a challenger writer prompt (kept only if it still carries the
grounding rules and is not bloated), saved in Langfuse labelled `challenger`. Champion and challenger
write for the same cases plus the fixed golden set. The challenger is promoted only if **all** hold:

1. zero invalid fact citations (hard gate: one is enough to reject);
2. higher average score (critic score, similarity to the human's edit, and a zero for repeating a rejected behaviour);
3. faithfulness rate does not drop;
4. at most 5% of cases regress;
5. it does not lose on the golden set.

Every cycle writes a `prompt_promotions` row with before/after numbers and the reasons. A human can pin
a prompt (`PINNED_PROMPTS=writer`) or switch the loop off (`LEARN_ENABLED=false`). Human edits that add
numbers or names the draft and facts did not contain are recorded as ungrounded and never used as targets.

## KPIs (template: not yet measured)

| KPI | Target | Measured |
|---|---|---|
| ICP precision / recall (golden set) | ≥ 85% / ≥ 80% | not run |
| Faithfulness | 100% | not run on real models |
| Approval rate without edits | rises after each promotion | not run |
| Average edit distance | falls after each promotion | not run |
| Cost per qualified lead | < $0.05 | canned demo only; prices in `config.py` come from Together's table (cached-input discounts ignored) |
| p95 time per lead | < 60 s | not measured with real models |

The control panel's KPI page and `sdr_loop.kpis` compute these per prompt version from the database.

## Layout

```
src/sdr_loop/  cli, pipeline, llm (the only model gateway), budget, observability, memory (DB),
               steps/ (extract qualify research write critic approve outbox), tools/, learn/, bot/, api/,
               ui/ (control panel), jobs (background runs), health (setup checks)
evals/         golden set + real-model evals
prompts/       seed prompts (also the offline fallback) alembic/ migrations     infra/ Dockerfile
docs/          PRD, decisions.md, journal.md          CLAUDE.md   rules for agent-written changes
```
