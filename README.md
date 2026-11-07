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
| **Not yet run with real services** | The Anthropic / Together providers, Langfuse, the Telegram bot against Telegram, the Evaboot API. Tests use scripted fake models and recorded fixtures; the first real run may surface integration bugs. |
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
- **The critic is a different model family** from the writer (Claude writes; Qwen on Together critiques;
  Llama on Together qualifies). An uncited claim caps faithfulness at 2 in code, not by trusting the model.
- **Budgets stop runs cleanly.** Per-lead (USD, tokens, 3 tool calls) and per-run (USD). A stopped lead
  never produces a partial draft. Runs resume by stage after a crash.
- **Nothing leaves without a human.** The outbox refuses any draft without an approval record. The bot
  answers only `TELEGRAM_CHAT_ID`. `DRY_RUN=false` refuses to start (v1 has no sending adapters).

## Architecture

How the app is built, as opposed to how a lead flows through it (the diagram above).

<p align="center">
  <img src="architecture.svg" alt="Layered architecture: entry points (CLI, FastAPI webhook, Telegram bot, Streamlit) call the application layer (pipeline orchestrator with seven steps, read-only research tools, learning loop), which uses a platform layer (the single LLM gateway, Langfuse tracing, the repository, prompt management) backed by Anthropic, Together AI, Langfuse Cloud, PostgreSQL and local files, plus a stack strip" width="100%">
</p>

| Layer | Choice |
|---|---|
| Language and runtime | Python 3.12, [uv](https://docs.astral.sh/uv/), `asyncio` (5 leads in flight) |
| Data and validation | Pydantic v2 (domain types, settings, and the schemas model output must satisfy), SQLAlchemy 2.0 async, Alembic, PostgreSQL 16 (SQLite for tests and local mode) |
| HTTP | FastAPI + uvicorn for the Evaboot webhook; `httpx` for Evaboot, Together AI and the research tools; `tenacity` for retries |
| Models | Anthropic SDK (Claude writes and researches) and Together AI over its OpenAI-compatible API (Llama qualifies, Qwen critiques), all behind one gateway |
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
uv sync --extra ui
cp .env.example .env            # keys are only needed for real-model runs

# 1. See the plumbing with NO keys (canned model outputs: not real quality)
uv run sdr-loop run --fixture --fake-llm --limit 20
uv run streamlit run app/streamlit_app.py     # review queue + KPIs

# 2. Real models (ANTHROPIC_API_KEY, TOGETHER_API_KEY, Langfuse keys in .env)
uv run sdr-loop prompts push                  # seed Langfuse prompt management
uv run sdr-loop run --fixture --limit 20
uv run sdr-loop bot                           # drafts arrive on Telegram

# 3. Live Evaboot (EVABOOT_API_KEY, PUBLIC_WEBHOOK_URL, and `sdr-loop api` reachable)
uv run sdr-loop run "https://www.linkedin.com/sales/search/people?..."
```

Docker (Postgres + migrations + webhook API + bot):

```bash
docker compose up -d --build                         # needs TELEGRAM_* in .env for the bot
docker compose run --rm cli run --fixture --fake-llm # or without --fake-llm for real models
```

Postgres is published on `127.0.0.1:55432` for dev tools; the API on `${API_PORT:-8000}`.

| Command | What it does |
|---|---|
| `sdr-loop run [URL] [--fixture] [--fake-llm] [--limit N] [--resume RUN_ID]` | extract → qualify → research → write → critic |
| `sdr-loop bot` | Telegram approval bot (Approve / Edit / Reject, daily reminder) |
| `sdr-loop api` | Evaboot webhook receiver (`POST /webhooks/evaboot/{secret}/{run_id}`) |
| `sdr-loop learn` | one learning cycle (needs ≥ 10 new edits/rejections) |
| `sdr-loop purge --lead <id or LinkedIn URL>` | delete the lead, derived data, outbox lines and trace |
| `sdr-loop prompts push` | seed Langfuse with the local prompt files |

Configuration is environment variables (see `.env.example`): budgets (`PER_LEAD_USD_CAP`, `PER_RUN_USD_CAP`,
`MAX_TOOL_CALLS`), `CONCURRENCY`, `LEARN_ENABLED`, `PINNED_PROMPTS`, `OFFLINE`. The ICP lives in
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
uv run pytest -m eval          # real models on the golden set (needs both API keys)
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
| Cost per qualified lead | < $0.05 | canned demo only; Together prices in `config.py` are approximate |
| p95 time per lead | < 60 s | not measured with real models |

The Streamlit KPI page and `sdr_loop.kpis` compute these per prompt version from the database.

## Layout

```
src/sdr_loop/  cli, pipeline, llm (the only model gateway), budget, observability, memory (DB),
               steps/ (extract qualify research write critic approve outbox), tools/, learn/, bot/, api/
app/           Streamlit review queue + KPI page      evals/   golden set + real-model evals
prompts/       seed prompts (also the offline fallback) alembic/ migrations     infra/ Dockerfile
docs/          PRD, decisions.md, journal.md          CLAUDE.md   rules for agent-written changes
```
