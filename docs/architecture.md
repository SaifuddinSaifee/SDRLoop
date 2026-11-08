# Architecture

Two views of the same system. Use the simple one to explain the idea in a minute, and the detailed
one to walk through the code. Both are Mermaid, so GitHub renders them and they're edited as text.

## Simple: what happens to a lead

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

Three rules hold the whole thing together:

1. **Facts are quotes.** Research keeps a fact only if it appears word for word on a page the
   agent actually fetched. Every personal line in an email must cite one.
2. **A second model checks the first.** The critic is from a different model family than the
   writer, and an unsupported claim fails the draft, whether it's about the prospect or about us.
3. **Nothing goes out without a human.** Sending requires an approval record.

Everything is traced in Langfuse: one trace per lead, with every step, tool call, score, decision
and cost.

## Detailed: how it's built

```mermaid
flowchart TB
    subgraph People["Who uses it"]
        OP["Operator / CEO"]
    end

    subgraph Interfaces["Interfaces"]
        UI["Control panel (Streamlit)<br/>Setup wizard · Home · Review queue<br/>Sent emails · Learning · KPIs"]
        TG["Telegram bot<br/>Approve / Edit / Reject<br/>/run · /status · /learn"]
        CLI["CLI: sdr-loop<br/>start · run · learn · check · purge"]
        API["Webhook API (FastAPI)<br/>POST /webhooks/evaboot/…"]
    end

    subgraph Runtime["sdr-loop start (one command)"]
        HEALTH["Setup checks + auto-fixes<br/>health.py"]
        BG["Background loop<br/>due emails · nightly learning"]
        JOBS["Background jobs<br/>one run or learn at a time<br/>jobs.py"]
    end

    subgraph Pipeline["Pipeline: pipeline.py, 5 leads in parallel, resumable per lead"]
        direction LR
        X["1 Extract<br/>dedup · email must be safe"] --> QL["2 Qualify<br/>rules, then LLM score"]
        QL --> RS["3 Research<br/>the one free agent<br/>max 3 tool calls"]
        RS --> WR["4 Write<br/>cites fact ids"]
        WR --> CR["5 Critic<br/>code checks, then LLM<br/>rewrite once, else flag"]
        CR --> AP["6 Approve<br/>human decision"]
        AP --> OB["7 Outbox + send<br/>needs approval record"]
    end

    subgraph Learn["Learning loop: learn/"]
        EV["Eval cases<br/>from edits + rejections"] --> CH["Challenger prompt"]
        CH --> PR["Champion vs challenger<br/>promote only if higher score<br/>AND zero invalid citations"]
    end

    subgraph Platform["Platform: single doors"]
        GW["LLM gateway: llm.py<br/>routing · retries · fallback<br/>JSON validation · budgets · cost"]
        TOOLS["Research tools (read-only)<br/>fetch_page: company site only<br/>search_news · read_lead"]
        MEM["Repository: memory.py<br/>the only DB access"]
        OBS["Tracing: observability.py<br/>PII masked"]
        PROMPTS["Prompts: prompts.py<br/>production / challenger"]
        CAMP["Campaign brief + ICP<br/>campaign.yaml · icp.yaml"]
        MAIL["Mailer: mailer.py<br/>local Mailpit only"]
    end

    subgraph External["External services and storage"]
        TOGETHER["Together AI<br/>GLM-5.3 writes · GLM Flash qualifies + researches<br/>DeepSeek critiques"]
        EVA["Evaboot API<br/>or local simulator"]
        WEB["Company websites<br/>+ Google News RSS"]
        LF["Langfuse<br/>traces · scores · prompt versions"]
        DB[("SQLite / PostgreSQL")]
        MP["Mailpit<br/>demo inbox"]
    end

    OP --> UI & TG
    UI & TG --> JOBS
    CLI --> Pipeline
    JOBS --> Pipeline
    JOBS --> Learn
    BG --> MAIL
    BG --> JOBS
    API --> MEM
    TG --> AP
    UI --> AP
    UI --> CAMP

    X --> EVA
    QL & RS & WR & CR --> GW
    CH & PR --> GW
    RS --> TOOLS --> WEB
    GW --> TOGETHER
    QL & WR & CR --> CAMP
    Pipeline --> MEM
    Learn --> MEM
    MEM --> DB
    GW --> OBS --> LF
    PROMPTS --> LF
    OB --> MAIL --> MP
    AP -.->|edits + rejections| EV
    PR -.->|new production label| PROMPTS

    classDef gate fill:#e3efff,stroke:#2f6fd0,color:#000
    classDef untrusted fill:#ffe5e5,stroke:#d03030,color:#000
    classDef learn fill:#efe4ff,stroke:#7a4fd0,color:#000
    classDef human fill:#fff4d6,stroke:#d9a400,color:#000
    class GW gate
    class TOOLS,WEB untrusted
    class EV,CH,PR learn
    class AP,OP human
```

Colours:
- **Blue:** the one door to the models.
- **Red:** where untrusted web text enters. It's wrapped as data and never treated as instructions.
- **Purple:** the learning loop.
- **Yellow:** the human.

### Why it's built this way

| Choice | Why |
|---|---|
| Fixed pipeline, one free agent | Predictable cost and easy step-level debugging. Only research needs to choose its own actions, so only research gets tools, and those are read-only and budgeted. |
| One LLM gateway | Retries, fallbacks, budgets, JSON validation and tracing in one place, so no step can skip them. |
| Facts validated in code | The model can't invent a source: a fact survives only if it's a substring of real tool output. |
| Critic from another model family | A model grading its own family's style is too lenient. |
| Brief as data, not prompt | The same (learned) prompts work for any company; claims about us get the same hard gate as claims about the prospect. |
| Human approval required | Sending is impossible without a decision record, and every decision is a training signal. |
| Runs as background jobs | A page reload or a bot restart never kills a run; the CLI stays the single code path. |
| Idempotent by lead | A crashed or budget-stopped run resumes where it stopped; nothing is sent twice. |

Every decision with its date is in [decisions.md](decisions.md).
