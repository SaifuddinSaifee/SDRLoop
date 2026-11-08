# Demo script

About 10 minutes. Everything is real except the Evaboot extraction step (see "Constraints" below).

## Before you record

```bash
uv run sdr-loop start          # all Setup checks should be ✅
```

- Setup → **Fresh start**: type RESET and click Clear, so the run starts from zero. Do this before
  every take, because the same search de-duplicates against leads already stored.
- Clear the Telegram chat, and keep http://localhost:8025 (Mailpit) open in a browser tab.
- Optional: set `FOLLOWUP_DELAY_MINUTES=1` on the Setup page so follow-ups arrive while you record.

## The run

1. **Setup page: what the agent knows.**
   - *Company & offer*: who the emails are from, and the **allowed / never claim** lists, taken
     from evaboot.com. Point out that the writer can only say what's on the allowed list.
   - *Who to target*: the ICP rules.
   - *Real leads*: the saved Sales Navigator search.

2. **Home → Start run** (live search, limit 30).
   - The progress bar shows "Evaboot is extracting your Sales Navigator search…" (about 20 s).
   - Then it switches to "N of 22 leads done". 26 people come back; 4 are skipped at once because
     their email isn't `safe` (riskier / suspect / invalid / none).
   - A Telegram message arrives when the run finishes: "✅ Run on Sales Navigator search…".

3. **Telegram: drafts arrive one by one** while the run is still going. Each draft has
   Approve / Edit / Reject buttons.
   - **Approve** one: the reply says email 1 is on its way to the inbox.
   - **Edit** one: reply with your version (email 1, a line with `---`, email 2).
   - **Reject** one: pick a one-tap reason (e.g. *bad tone*).

4. **Review queue** (control panel): the draft beside the **cited sources**. Each fact is a
   quote from the company's real website, with its link. The critic's four scores sit below.
   - Show a **flagged** draft. In the test run it was SalesHive: the writer invented "reps pull
     from Sales Navigator manually" and the critic caught it. Also folk, flagged as a
     non-English market.

5. **Qualification**: on the KPIs page, or in Langfuse, show who was filtered out and why:
   - too big: CIENCE, Callbox, Morgan McKinley
   - competitors: lemlist, Waalaxy
   - excluded titles: engineer, assistant
   - judged by the AI to be competitors: Smartlead, LeadFuze ("sells lead lists… likely a
     competitor")

6. **Sent emails + Mailpit**: email 1 is in the inbox, From "Saif <saif@evaboot.test>", To the
   lead. Click **Send follow-ups now**, and email 2 appears threaded under it as a reply.

7. **Langfuse** (jp.cloud.langfuse.com): one trace per lead. Show the steps (qualify, research
   with each tool call and its URL, write, critic, approve, outbox, send), the scores, the cost,
   and the prompt versions in use.

8. **Learning**: the loop needs at least 10 edits or rejections. Approvals don't count, because
   they teach nothing. After reviewing, open Learning → **Run a learning cycle now**. The
   challenger prompt is compared with the champion on your edits, and the promotion table shows
   whether it won and why.

9. **KPIs**: approval rate, edit distance and critic pass rate per prompt version, and cost per
   qualified lead (target under $0.05; the test run was about $0.007).

## Constraints to say on camera

- **Evaboot is simulated.** Extraction needs a LinkedIn Sales Navigator seat connected to Evaboot,
  plus credits. The app speaks Evaboot's documented API to a local stand-in
  (`EVABOOT_MODE=simulated`). Switching to the real API is one setting: Setup → Real leads → Live.
- **Real companies, fictional people.** Company websites and news are real and read live; names,
  titles and email addresses are made up.
- **Nothing reaches anyone.** Emails go to Mailpit, a local inbox. The app refuses any non-local
  mail server in code.
- **Everything else is real**: GLM-5.3 / DeepSeek on Together AI, live web research, Telegram,
  Langfuse.
