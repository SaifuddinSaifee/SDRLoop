# PRD: Self-improving SDR agent (evaboot-sdr-loop)

Nov 7, 2026 · @MuSaif

## Overview

We are building a Python SDR agent that takes a LinkedIn Sales Navigator search, gets clean leads through the Evaboot API, qualifies them, researches them, writes a personalised two-email sequence and sends it to a human for approval on Telegram. Every human edit or rejection becomes a test case, and the agent only changes its own prompts when a new version scores better on those tests.

- **Goal:** a working, observable, self-improving outreach agent in a public repo, built in about 7 days, that shows strong Python, agentic coding and agentic plumbing.
- **Why now:** the Evaboot Agentic Python Engineer application asks to be shown a Python agentic project and walked through its architecture, observability, quality checks and lessons.
- **Demo ICP:** Evaboot's own customers (lead-gen agencies, SDR teams, recruiters who prospect in Sales Navigator). The agent runs in dry-run mode, so no real emails are sent.
- **Owner:** Saifuddin Saifee. Repo: `evaboot-sdr-loop`.

## User problem

An SDR's day is a loop of slow manual steps: find leads, check fit, read about each company, write a personal email, follow up and learn from who replies. Evaboot automates only the first step. It turns a Sales Navigator search into a clean list with verified emails, and the other steps are still done by hand.

**What hurts today**

- **Qualification is slow and inconsistent.** Reps skim titles and company pages, and two reps score the same lead differently.
- **Personalisation does not scale.** A good personal first line needs real research on every lead, so reps fall back to templates that get ignored.
- **AI writers make things up.** Generic AI email tools invent facts about the prospect ("congrats on your Series B"), which damages the sender's reputation.
- **Nothing learns.** When a rep rewrites an AI draft, that correction is lost. The tool makes the same mistake tomorrow.
- **No visibility.** Teams cannot see what each email costs, why a lead was rejected, or whether the tool is getting better.

**Who has this problem:** SDRs and founders doing outbound, lead-gen agencies running campaigns for clients, and recruiters doing candidate outreach. These are Evaboot's customers.

## Goals, non-goals and success metrics

**Goals**

1. Turn a Sales Navigator search into approved, personalised two-email sequences with no manual research.
2. Every claim in an email is backed by a source the agent actually read.
3. The agent gets measurably better from human feedback, and never gets worse without someone noticing.
4. Every run is fully observable: cost, latency, decisions and quality scores per lead.

**Non-goals (v1)**

- Sending real emails to real people. v1 is dry run by default.
- Scraping LinkedIn directly. Lead data comes only from the Evaboot API or its CSV export.
- Reply handling for live campaigns. Reply outcomes are simulated from a labelled set.
- A full web app. Approvals happen on Telegram and review in a small Streamlit page.
- Multi-user accounts, billing or a CRM sync.

**Success metrics (targets for the demo run of 200 to 500 leads)**

| KPI | How it is measured | Target |
| --- | --- | --- |
| ICP precision | Leads marked fit that a human also marks fit, on the golden set | 85% or higher |
| ICP recall | Fit leads the agent catches, on the golden set | 80% or higher |
| Faithfulness | Email claims with a valid source fact id | 100% (CI fails below) |
| Approval rate without edits | Drafts approved as written / all drafts reviewed | Goes up after each prompt promotion |
| Average edit distance | Characters changed by the human per approved draft | Goes down after each prompt promotion |
| Cost per qualified lead | Total LLM and tool cost / leads that pass qualification | Under $0.05 |
| p95 time per lead | Extraction excluded, qualify to draft ready | Under 60 seconds |

The targets are starting guesses to check on day 1. The README reports the real numbers, before and after one prompt promotion.

## Users and user stories

**Primary user: the operator** (an SDR, a founder doing outbound, or an agency running campaigns). For the demo, this is you.

**Secondary user: the builder** (the engineer who owns the agent, tunes it and is on the hook for its numbers).

| ID | As a... | I want to... | So that... |
| --- | --- | --- | --- |
| US1 | Operator | paste a Sales Navigator search URL and start a run | I get a qualified, personalised sequence per lead without manual research |
| US2 | Operator | see why each lead was marked fit or not fit | I trust the qualification and can correct it |
| US3 | Operator | approve, edit or reject each draft from Telegram on my phone | nothing goes out without me, and review takes seconds |
| US4 | Operator | open a longer draft next to the sources it cited | I can check facts before approving |
| US5 | Operator | describe my ICP in a plain YAML file | I can reuse the agent for another product or client |
| US6 | Builder | see every run as a trace with cost, latency and scores per step | I can find where quality or cost goes wrong |
| US7 | Builder | have my edits and rejections turned into test cases automatically | the agent learns from real corrections |
| US8 | Builder | have a new prompt version replace the old one only if it scores better | the agent never quietly gets worse |
| US9 | Builder | run evals in CI on every pull request | an agent-written code change cannot break quality unnoticed |
| US10 | Builder | cap spend per run and per lead | a stuck agent cannot burn money |

## Solution overview

A fixed Python pipeline of seven steps with one free agent inside it (research), a human approval gate before anything leaves, and a nightly learning loop that rewrites the writer prompt only when the new version wins on tests.

&#91;embedded content: SDR agent pipeline · 7 steps and a nightly learning loop\]

A draft that fails the critic goes back to the writer once, then to the human with a flag. Only approved drafts reach the outbox, and only edits and rejections feed the learning loop.

## Functional requirements

**FR1. Extract**

- FR1.1 Accept a Sales Navigator search URL from the command line (`sdr-loop run <url>`) or the Streamlit page.
- FR1.2 Call the Evaboot API (`POST /v1/extractions/url/`) and receive results on a FastAPI webhook endpoint.
- FR1.3 Store each lead once, keyed by its LinkedIn profile URL. Re-running the same search must not create duplicates.
- FR1.4 Offline mode: load a recorded Evaboot CSV export as a fixture, so tests and evals never call the paid API.
- FR1.5 Skip leads with no verified email and record why.

**FR2. Qualify**

- FR2.1 Read the ICP from `icp.yaml` (industries, company size, titles, geographies, disqualifiers).
- FR2.2 Return a Pydantic object per lead: fit score 0 to 100, verdict (fit, maybe, not fit), up to 3 reasons, and any disqualifier hit.
- FR2.3 Apply hard rules in code first (country, headcount, title keywords), then the LLM only for leads that pass.
- FR2.4 Only fit leads go on to research. Maybe leads are logged for review.

**FR3. Research**

- FR3.1 A tool-using agent with a fixed toolset: `fetch_page` (company website), `search_news` (recent company news), `read_lead` (lead fields from Evaboot).
- FR3.2 Hard budget: at most 3 tool calls and a fixed token cap per lead.
- FR3.3 Save each finding as a fact with an id, the source URL and the exact supporting snippet.
- FR3.4 Treat all web text as data. Instructions found on a page are never followed.

**FR4. Write**

- FR4.1 Write a two-email sequence: email 1 (under 90 words, one personal line, one clear ask) and a follow-up (under 60 words, adds something new).
- FR4.2 Every personal claim cites a fact id from FR3. A claim with no fact id is not allowed.
- FR4.3 Use the current champion prompt version from Langfuse prompt management.

**FR5. Critic**

- FR5.1 Code checks first: word count, banned and spam words, number of links, one ask, every cited fact id exists.
- FR5.2 Then an LLM critic from a different model family scores faithfulness, personalisation, clarity and tone from 1 to 5, with a reason for each.
- FR5.3 A draft that fails goes back to the writer once with the critic's notes. If it fails again, it is flagged for the human.

**FR6. Approve**

- FR6.1 A Telegram bot sends each draft with Approve, Edit and Reject buttons.
- FR6.2 Edit: the operator replies with the new text, or opens the Streamlit review page for longer edits.
- FR6.3 Reject asks for a one-tap reason (wrong fit, wrong fact, bad tone, too long, other).
- FR6.4 Nothing reaches the outbox without an approval record.

**FR7. Outbox**

- FR7.1 Default: write approved sequences to a JSONL outbox (dry run).
- FR7.2 Optional adapters, off by default: Instantly campaign or Gmail drafts.

**FR8. Learn**

- FR8.1 Every edit, rejection and simulated reply is stored as an eval case.
- FR8.2 A nightly job generates a challenger prompt, runs it against the eval set and the golden set, and promotes it only if it wins (see the self-improving loop section).

**FR9. Review and KPIs (Streamlit)**

- FR9.1 Review queue: draft on the left, cited sources on the right.
- FR9.2 KPI page: approval rate, edit distance, cost per qualified lead, critic pass rate, per prompt version.

## Self-improving loop

The agent changes its own prompts only through a champion and challenger test, so it can get better but cannot quietly get worse.

1. **Collect signals.** Every human decision is stored: approved as written, edited (with the before and after text), or rejected (with the reason). Simulated replies come from a labelled set of 50 example replies (interested, not now, wrong person, unsubscribe).
2. **Turn signals into eval cases.** Each edit becomes a case: input = lead + facts, expected = the human's final text, scored on edit distance and on the critic rubric. Each rejection becomes a negative case the new prompt must not repeat.
3. **Generate a challenger.** A nightly job reads the last batch of edits and rejections, asks an LLM to propose a revised writer prompt that addresses the patterns (for example "always too long", "opens with flattery"), and saves it as a new version in Langfuse, labelled challenger.
4. **Run both on the same sets.** Champion and challenger write drafts for the eval set and the golden set. The critic and the code checks score both.
5. **Promote only on a win.** The challenger is promoted when it beats the champion on the average score, with no drop in faithfulness, and no regression on more than 5% of cases. Otherwise it is archived with its scores.
6. **Record it.** Every promotion is a Langfuse prompt version plus a row in `prompt_promotions` with the before and after numbers. The Streamlit KPI page shows the history.

**Guardrails on the loop**

- Faithfulness is a hard gate: a challenger that cites one fact that does not exist is rejected, whatever its other scores.
- The golden set stays fixed, so the agent cannot overfit to the latest edits alone.
- A human can pin a version and switch the loop off with one flag (`LEARN_ENABLED=false`).
- The same idea applies later to the qualify prompt: human corrections of fit verdicts become qualification eval cases.

## Observability and evaluation

Every lead is one Langfuse trace, every step is a span, and every quality signal is a score on that trace. A wrong email can be traced back to the exact fact, tool call or prompt version that caused it.

**Tracing (Langfuse)**

- One trace per lead, tagged with run id, prompt versions and ICP version.
- One span per step (extract, qualify, research, write, critic, approve, outbox), with model, input and output tokens, cost and latency.
- Tool calls in research are child spans with the URL fetched and the bytes returned.
- Scores attached to the trace: ICP score, critic scores (faithfulness, personalisation, clarity, tone), code-check pass or fail, human decision, edit distance.
- Errors and retries are visible as span events, not hidden in logs.

**Evaluation**

| Eval | Data | Metric | Where it runs |
| --- | --- | --- | --- |
| Qualification | Golden set of 40 to 60 leads labelled by hand | Precision and recall on fit | pytest, CI on every PR |
| Faithfulness | Golden set drafts | Share of claims with a valid fact id and a supporting snippet | pytest, CI on every PR (hard gate) |
| Writing quality | Golden set drafts | Critic rubric average and code checks | pytest, CI on every PR |
| Learning | Eval cases from human edits and rejections | Edit distance and rubric, champion vs challenger | Nightly job, results in Langfuse |
| Critic agreement | 20 drafts also scored by you | How often the critic agrees with your score | Once on day 4, then weekly |

**Critic agreement matters:** if the critic disagrees with you on many drafts, its scores cannot be trusted to promote prompts. That check is what makes the loop honest.

**KPI view:** the Streamlit KPI page reads from Postgres and shows approval rate, edit distance, cost per qualified lead and critic pass rate per prompt version. Langfuse dashboards cover cost and latency per step.

## Non-functional requirements

| Area | Requirement |
| --- | --- |
| Safety | Dry run by default. No email leaves without an approval record. Real-send adapters need an explicit flag and are off in the demo. |
| Security | API keys only in environment variables or AWS Secrets Manager, never in code or logs. Web content is treated as data (prompt injection containment). The research agent has read-only tools. The Telegram bot answers only your chat id. |
| Privacy | Lead personal data stays in your database. Langfuse traces mask email addresses and phone numbers. A `purge --lead` command deletes a lead and its traces. |
| Cost | Budget caps per lead (tokens and tool calls) and per run (USD). A run stops cleanly when the cap is hit. Cheap model for qualify, stronger model only for write. |
| Reliability | Every external call has a timeout, retries with backoff and a fallback model. Steps are idempotent by lead id, so a crashed run resumes where it stopped. Evaboot webhooks are verified and processed once. |
| Performance | Leads are processed concurrently (asyncio, limit of 5 at a time). p95 under 60 seconds per lead from qualify to draft ready. |
| Code quality | Typed Python 3.12 (mypy strict on `src/`), ruff, pytest. CI runs lint, types, unit tests and evals on every PR. |
| Agentic coding | Built with Claude Code. `CLAUDE.md` holds the rules (tests first, no secrets, every LLM call through `llm.py`). The PRD and a decisions log live in `docs/`. |
| Portability | Runs locally with Docker Compose (Postgres, app). One scheduled run deployed on AWS. |

## Architecture, data model and tech stack

A fixed pipeline with one free agent (research) inside it. The pipeline is plain Python so it is cheap, predictable and testable. Only research gets tools and a choice of what to do, because that is the one step where the path really differs per lead.

**Key design decisions**

- **Fixed pipeline over a fully autonomous agent:** predictable cost per lead, easy to test step by step, and failures point to one step.
- **One LLM gateway (`llm.py`):** every model call goes through it for retries, fallbacks, model routing, budget checks and Langfuse spans. No other file may call a model directly.
- **Critic from a different model family than the writer:** a model grading its own style is too lenient.
- **Facts with ids:** grounding is checked in code, not trusted to the prompt.
- **Telegram for approval, not a dashboard:** approval should happen where the operator already is, in seconds.

**Code layout**

```
src/sdr_loop/
  cli.py              # sdr-loop run | learn | purge
  pipeline.py         # orchestrates steps, one Langfuse trace per lead
  steps/              # extract, qualify, research, write, critic, approve, outbox
  tools/              # fetch_page, search_news, read_lead (typed, budgeted)
  llm.py              # gateway: routing, retries, fallbacks, cost, spans
  memory.py           # repository layer over Postgres
  learn/              # build_eval_set, challenger, promote
  bot/telegram.py     # approval bot
  api/webhooks.py     # FastAPI: Evaboot webhook
app/streamlit_app.py  # review queue + KPI page
evals/                # golden_leads.jsonl, test_qualify.py, test_write.py
docs/                 # PRD.md, CLAUDE.md, decisions.md, journal.md
infra/                # Dockerfile, docker-compose.yml, AWS task definition
```

**Data model (Postgres)**

| Table | Key fields |
| --- | --- |
| `runs` | id, search\_url, icp\_version, status, started\_at, cost\_usd |
| `leads` | id, linkedin\_url (unique), name, title, company, email, email\_status, run\_id |
| `qualifications` | lead\_id, score, verdict, reasons, prompt\_version |
| `facts` | id, lead\_id, source\_url, snippet, kind |
| `drafts` | id, lead\_id, email\_1, email\_2, cited\_fact\_ids, prompt\_version, critic\_scores, status |
| `decisions` | draft\_id, action (approve, edit, reject), final\_text, reason, edit\_distance, decided\_at |
| `eval_cases` | id, source (edit, reject, reply, golden), input, expected, created\_at |
| `prompt_promotions` | prompt\_name, from\_version, to\_version, champion\_score, challenger\_score, promoted, created\_at |

**Tech stack**

| Layer | Choice |
| --- | --- |
| Language | Python 3.12, uv, asyncio |
| Data and validation | Pydantic v2, SQLAlchemy 2.0, Alembic, PostgreSQL (SQLite for tests) |
| API and webhooks | FastAPI, httpx |
| LLMs | Two model families via one gateway (for example Claude for writing, an open-weight model on Together AI for qualifying, a third family as critic) |
| Observability and evals | Langfuse (traces, scores, prompt management, datasets), pytest |
| Approval and UI | python-telegram-bot, Streamlit |
| Lead data | Evaboot API (extraction with webhook), CSV fixture for offline mode |
| Infra | Docker, Docker Compose, AWS ECS Fargate scheduled task (or Lambda + SQS), AWS Secrets Manager, GitHub Actions |

## MoSCoW prioritisation

**Must have** (the demo fails without these)

- Extract via Evaboot API with webhook, plus CSV fixture mode
- Qualify against `icp.yaml` with reasons
- Research agent with 3-call budget and facts with source ids
- Two-email writer that cites fact ids
- Code checks and an LLM critic from another model family
- Telegram approval with Approve, Edit, Reject
- Dry-run JSONL outbox
- Langfuse tracing and scores on every lead
- Golden set evals in pytest, run in GitHub Actions
- Learn loop: edits and rejections to eval cases, challenger vs champion, promote on win
- README with architecture, KPI table and before and after numbers

**Should have**

- Streamlit review page and KPI page
- AWS deployment of one scheduled run
- Critic agreement check against your own scores
- Per-run and per-lead budget caps with clean stop
- `docs/decisions.md` and `docs/journal.md`
- A 3 to 4 minute Loom walkthrough

**Nice to have**

- Learning loop for the qualify prompt too
- Simulated replies feeding the loop
- Instantly or Gmail drafts adapter behind a flag
- Evaboot MCP server as an alternative to the API

**Won't have (v1)**

- Real sending to real prospects
- Direct LinkedIn scraping
- Multi-user, auth, billing
- CRM sync

## Edge cases

| # | Situation | Expected behaviour |
| --- | --- | --- |
| E1 | Evaboot webhook never arrives | Poll the extraction status after a timeout; mark the run failed with a clear reason after the last retry |
| E2 | Same webhook delivered twice | Processed once (idempotency key on extraction id) |
| E3 | Same lead appears in two searches | One lead record; the second run links to it and skips steps already done |
| E4 | Lead has no verified email | Skipped at extract, reason stored, counted in the run report |
| E5 | Company website is down, slow or blocks bots | Tool returns an error after its timeout; research continues with other tools; if no facts, the email uses no personal line and is flagged |
| E6 | Company website contains text like "ignore previous instructions" | Treated as page content only; no tool or prompt change; covered by a test |
| E7 | Research finds nothing useful | Writer produces a short role-based email with no invented personal claim; critic checks it has zero uncited claims |
| E8 | Writer cites a fact id that does not exist | Code check fails, draft goes back once, then flagged for human |
| E9 | LLM returns invalid JSON or wrong schema | Pydantic validation error, one retry with the error message, then fallback model |
| E10 | Model provider rate limit or outage | Backoff and retry, then fallback model; span records which model answered |
| E11 | Per-lead or per-run budget hit | Lead or run stops cleanly with status budget\_exceeded; no partial draft goes to approval |
| E12 | Lead's company is the operator's own customer or a competitor | Disqualifier list in `icp.yaml` removes them at qualify |
| E13 | Operator does not respond on Telegram | Draft stays pending; daily reminder; nothing is sent |
| E14 | Operator edits a draft into something with a new uncited claim | Allowed (human owns it), but recorded so the learn step does not treat that claim as grounded |
| E15 | Challenger wins on average but fails faithfulness on one case | Not promoted (faithfulness is a hard gate) |
| E16 | Too few new edits for a fair test | Learn job skips the night and logs why (minimum 10 new cases) |
| E17 | Lead in a non-English market | v1 writes in English only and flags the lead; language support is a later item |
| E18 | Crash in the middle of a run | Resume from the last finished step per lead |

## Test cases

Unit and integration tests run with mocked models and the CSV fixture, so CI never calls a paid API. Eval tests run against real models on the golden set and are gated in CI.

| ID | Area | Test | Pass condition |
| --- | --- | --- | --- |
| T1 | Extract | Load the CSV fixture | All leads parsed; leads with no verified email skipped with a reason |
| T2 | Extract | Send the same webhook twice | One set of leads stored |
| T3 | Extract | Re-run the same search | No duplicate leads; finished steps skipped |
| T4 | Qualify | Lead outside the ICP country | Rejected by code rule, no LLM call made |
| T5 | Qualify | Golden set (40 to 60 leads) | Precision 85% or higher, recall 80% or higher |
| T6 | Qualify | LLM returns malformed JSON | One retry, then fallback model, valid object returned |
| T7 | Research | Agent asks for a 4th tool call | Call refused, research ends with the facts so far |
| T8 | Research | Page with an injected instruction | Instruction not followed; facts only from page content; tools unchanged |
| T9 | Research | Website times out | Tool error recorded, research continues |
| T10 | Write | Draft for a golden lead | Every personal claim has a fact id that exists for that lead |
| T11 | Write | Lead with zero facts | Draft has no personal claims and is flagged |
| T12 | Critic | Draft over the word limit or with a spam word | Code check fails before the LLM critic runs |
| T13 | Critic | Draft with an invented fact | Faithfulness score 1 to 2, draft sent back to the writer |
| T14 | Approve | Approve on Telegram | Decision stored, sequence in the JSONL outbox |
| T15 | Approve | Message from an unknown Telegram chat id | Ignored, logged |
| T16 | Approve | Draft with no approval record | Never written to the outbox |
| T17 | Learn | 10 edits stored | 10 eval cases created with input and expected text |
| T18 | Learn | Challenger beats champion on all metrics | Promoted; row in `prompt_promotions`; new version labelled in Langfuse |
| T19 | Learn | Challenger wins but breaks faithfulness on one case | Not promoted |
| T20 | Budget | Per-run cap set to $0.10 | Run stops with status budget\_exceeded, no partial drafts sent to approval |
| T21 | Observability | One full lead run | One trace, 7 step spans, cost and tokens on each, all scores attached |
| T22 | Privacy | Run `purge --lead` | Lead, facts, drafts and traces deleted; emails masked in traces before that |

## Milestones and plan

About 7 working days (evenings plus one weekend). Each day ends with something that runs and a line in `docs/journal.md`.

1. **Day 1: Foundations.** PRD and `CLAUDE.md` in the repo, project skeleton with uv, ruff, mypy, pytest and GitHub Actions. Evaboot trial account, one real extraction recorded as the CSV fixture. Write `icp.yaml` and label 40 to 60 golden leads by hand. Langfuse project set up.
2. **Day 2: Extract and qualify.** Evaboot client and FastAPI webhook, lead storage with dedup, code rules plus LLM qualification with reasons. Tests T1 to T6 green. Every call traced from the first commit.
3. **Day 3: Research and write.** Tool registry with budgets, research agent, facts with ids, two-email writer citing facts. Tests T7 to T11 green.
4. **Day 4: Critic and evals.** Code checks, LLM critic from another model family, golden-set evals in CI, critic agreement check on 20 drafts you score yourself.
5. **Day 5: Approval.** Telegram bot with Approve, Edit, Reject; JSONL outbox; Streamlit review page. Tests T14 to T16 green.
6. **Day 6: Learn loop.** Eval cases from decisions, challenger generation, champion vs challenger run, promotion with Langfuse prompt versions. Do one real round: review about 50 drafts, run the loop, record before and after numbers.
7. **Day 7: Ship.** AWS deployment of one scheduled run, Streamlit KPI page, README with architecture diagram and KPI table, decisions log, and a 3 to 4 minute Loom walkthrough. Then submit the Evaboot form.

**Definition of done:** a stranger can clone the repo, run `docker compose up` and `sdr-loop run --fixture`, and see traces in Langfuse and drafts on Telegram within 10 minutes.

## Risks, open questions and expected impact

**Risks**

| Risk | Mitigation |
| --- | --- |
| Evaboot trial credits run out or the API needs a paid plan | Record one real extraction early and run everything else on the CSV fixture |
| The critic agrees with itself, not with a human | Critic agreement check on 20 drafts you score; switch critic model if agreement is low |
| The loop overfits to your own taste | Fixed golden set as a second gate; promotions need wins on both sets |
| Research agent burns budget on slow sites | Hard limits on tool calls, timeouts and per-lead spend |
| Scope creep past 7 days | MoSCoW is the line: ship the Must list, then the Should list |
| Sending to real people by mistake | Dry run by default; real adapters off and behind a flag; no real send in the demo |

**Open questions**

- Does the Evaboot free trial include API access, or only the Chrome extension? Check on day 1.
- Which three model families for qualify, write and critic? Decide after a small bakeoff on 10 golden leads on day 2.
- AWS: ECS Fargate scheduled task or Lambda + SQS? Pick the one you can deploy in half a day.

**Expected impact**

- **For an SDR team:** research and first drafts go from manual work per lead to a quick review on the phone, with every claim checkable.
- **For the builder:** every run is measurable, and the agent's quality goes up through tested prompt changes instead of guesswork.
- **For the application:** Robin sees a small version of the SDR agent he is hiring for, in Python, running on Evaboot's own API, with evals, Langfuse, AWS and a self-improving loop. The walkthrough answers come straight from the journal.
