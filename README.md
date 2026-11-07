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

```mermaid
flowchart LR
  A[Sales Nav URL / CSV fixture] --> E[1 extract<br/>Evaboot API + webhook]
  E --> Q[2 qualify<br/>code rules, then LLM]
  Q -->|fit| R[3 research<br/>agent, max 3 tool calls]
  Q -->|not fit / maybe| L[(logged)]
  R --> W[4 write<br/>cites fact ids]
  W --> C[5 critic<br/>code checks + other-family LLM]
  C -->|fail once| W
  C --> P[6 approve<br/>Telegram / Streamlit]
  P -->|approve / edit| O[7 outbox.jsonl<br/>dry run]
  P -->|edit / reject| EV[(eval cases)]
  EV --> N[nightly learn:<br/>challenger vs champion]
  N -->|wins + faithfulness gate| W
```

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
