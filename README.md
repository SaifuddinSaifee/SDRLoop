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

```mermaid
flowchart LR
    S["🔎 Sales Navigator<br/>search"] --> E["📇 Evaboot<br/>leads + verified emails"]
    E --> Q["🎯 Qualify<br/>is this a fit?"]
    Q --> R["🌐 Research<br/>their website + news"]
    R --> W["✍️ Write<br/>2-email sequence"]
    W --> C["🧐 Critic<br/>checks every claim"]
    C --> H["👤 You approve<br/>Telegram or web"]
    H --> M["📬 Send<br/>email + follow-up"]

    H -.->|edits and rejections| L["🧠 Learning loop<br/>tests a better prompt nightly"]
    L -.->|promotes only if it wins<br/>with zero invented claims| W

    classDef human fill:#fff4d6,stroke:#d9a400,color:#000
    classDef learn fill:#efe4ff,stroke:#7a4fd0,color:#000
    class H human
    class L learn
```

Facts are quotes from pages the agent actually fetched, a critic from a different model family
checks every claim, and nothing is sent without a human approval. **The detailed diagram is in
[`docs/architecture.md`](docs/architecture.md).** It covers the interfaces, `sdr-loop start` and its background
jobs, the seven pipeline steps, the learning loop, the single-door platform modules (LLM gateway,
tools, repository, tracing, prompts, campaign brief, mailer) and the external services, with the
reasoning behind each choice.

| Layer | Choice |
|---|---|
| Language and runtime | Python 3.12, [uv](https://docs.astral.sh/uv/), `asyncio` (5 leads in flight) |
| Data and validation | Pydantic v2 (domain types, settings, and the schemas model output must satisfy), SQLAlchemy 2.0 async, Alembic, PostgreSQL 16 (SQLite for tests and local mode) |
| HTTP | FastAPI + uvicorn for the Evaboot webhook; `httpx` for Evaboot, Together AI and the research tools; `tenacity` for retries |
| Models | Together AI only, over its OpenAI-compatible API, behind one gateway: GLM-5.3 (write, challenger), GLM-5.3 Flash (research, qualify), DeepSeek V4.1 Flash (critic) |
| Observability and prompts | Langfuse: traces, scores, and prompt versions with `production` / `challenger` labels |
| Approval and UI | `python-telegram-bot` (long polling, commands), Streamlit control panel (setup wizard, runs, review queue, sent emails, learning, KPIs) |
| Email (demo) | `smtplib` to a local [Mailpit](https://mailpit.axllent.org) inbox only; threaded follow-ups |
| Parsing and safety | `selectolax` (HTML to text), `defusedxml` (news RSS), `rapidfuzz` (edit distance) |
| Quality | `pytest` + `pytest-asyncio`, `ruff`, `mypy --strict` on `src/`, GitHub Actions |
| Packaging | Docker, Docker Compose (Postgres, migrations, Mailpit, API, control panel + bot via `sdr-loop start`, CLI) |

The structure enforces a few rules, each with exactly one home:

- **One door to models.** Only `llm.py` talks to a provider. Routing, retries, the fallback model, JSON-schema validation with a corrective retry, budget checks, cost accounting and the Langfuse generation span all live there, so no step can skip them.
- **A fixed pipeline with one free agent.** `pipeline.py` orchestrates plain-Python steps; only research chooses its own actions, and its toolset is frozen, read-only and budgeted. Steps are idempotent by lead, so a crashed or budget-stopped run resumes by stage.
- **One door to the database.** `memory.py` is the only module that issues queries; everything else works with typed objects.
- **Telegram is transport, not logic.** The approval rules live in `BotController`, which the control panel reuses, so a decision made on the web gets the same guards, tracing and outbox behaviour as one made on a phone.
- **Tests never touch a paid service.** `FakeProvider`, an offline HTTP transport serving recorded pages, and a SQLite database stand in for every dependency. The same suite also runs against Postgres.

Why these choices were made is in [`docs/decisions.md`](docs/decisions.md).

## Quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run sdr-loop start     # opens the control panel; its Setup page walks you through the rest
```

`sdr-loop start` is the only command you need day to day. It:

1. **Checks your setup and fixes what it can.** It creates the local database, starts the demo
   inbox (Mailpit, via Docker) when email delivery is on, saves your Telegram chat id as soon as
   you message the bot, and uploads missing or outdated built-in prompts to Langfuse.
2. **Opens the control panel** at http://localhost:8501:
   - **Setup:** a step-by-step wizard. (1) Company & offer: who the emails are from and the only
     claims the writer may make about you. (2) Who to target. (3) AI models. (4) Real leads:
     Evaboot key and your Sales Navigator searches. (5) Email delivery to the demo inbox.
     (6) Telegram. (7) Langfuse. Plus **Fresh start**, which clears leads, drafts and history.
     It opens automatically until the required steps pass.
   - **Home:** start a run (live Sales Navigator search or the recorded export), watch its
     progress live, and see setup status.
   - **Review queue:** each draft next to its cited sources, with Approve / Edit / Reject.
   - **Sent emails:** what went to the demo inbox, plus "send the follow-ups now".
   - **Learning:** run a learning cycle and see the promotion history.
   - **KPIs.**
3. **Runs the Telegram bot.** Drafts arrive on your phone within seconds of being written, and you
   get a message when each run finishes. From the phone you can also send `/run 20`,
   `/run <Sales Navigator URL> 20`, `/status` and `/learn`.
4. **Sends approved emails to the demo inbox.** Email 1 goes on approval, and email 2 follows
   after `FOLLOWUP_DELAY_MINUTES` (default 3, demo pace), threaded as a reply.
5. **Runs the learning cycle every night** at `LEARN_AT` (default 02:00 local time).

Ctrl-C stops it. A run that is already going finishes on its own, and a crashed run can be resumed.

**Demo delivery.** Approved emails are really sent over SMTP, but only to
[Mailpit](https://mailpit.axllent.org), a local inbox at http://localhost:8025 that catches
everything and delivers nothing. Leads, addresses and research can all be real, and nobody receives
an email. The guard is in code: `EMAIL_MODE` must be `mailpit`, and any SMTP host other than
localhost or `mailpit` is refused. v1 has no code path to a real mail server.

**Simulated Evaboot.** `EVABOOT_MODE=simulated` (Setup → Real leads) routes the real Evaboot
client to a local stand-in that speaks the documented API. It returns an export of **real
companies** (live websites, read by the research step) with **fictional people**, for demos
without a Sales Navigator seat or credits. Everything after extraction is unchanged. In live mode,
the Setup page shows your Evaboot credits and whether a Sales Navigator seat is connected. The
recording walkthrough is in [`docs/DEMO.md`](docs/DEMO.md).

**The campaign brief** ([`campaign.yaml`](campaign.yaml), edited in Setup → Company & offer)
tells the writer who it writes for. The prompts carry no company name. The default brief is
Evaboot, with claims taken from evaboot.com. Statements about us are held to the same rule as
statements about the prospect: the critic flags anything `allowed_claims` doesn't support.

What each key unlocks (the Home and Setup pages show which ones are working):

| `.env` | Without it | With it |
|---|---|---|
| `TOGETHER_API_KEY` | demo runs only (canned AI output, tick "Demo mode") | real qualify / research / write / critic |
| `TELEGRAM_BOT_TOKEN` | review in the control panel | review on your phone too |
| `LANGFUSE_*` | no traces | one trace per lead, scores, prompt versions |
| `EVABOOT_API_KEY` | runs on the recorded export only | real leads from your Sales Navigator searches (1 credit per lead) |
| `EMAIL_MODE=mailpit` | approved emails go to `outbox.jsonl` only | also sent to the local demo inbox |

Telegram setup: in Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, pick a
name and a username ending in `bot`, and paste the token in Setup → Telegram. Then press **Start**
in your bot. The chat id is found and saved for you, and the bot comes up without a restart. It
only ever answers that one chat. If a token leaks, send `/revoke` to @BotFather.

Docker (Postgres + migrations + control panel + bot + webhook API + Mailpit):

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
