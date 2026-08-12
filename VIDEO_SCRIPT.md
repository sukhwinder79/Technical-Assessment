# Video narration script

Word-for-word narration paired with what to do on screen. Target **8 minutes**.

**> DO** = the action. **> SAY** = read it out (paraphrase freely, it should sound like you).

Rules for yourself while recording:

- Read numbers **off the screen**, never from this script — the agent picks its own row counts.
- Say **"writes into a Google Sheet"**, not "creates one". You are in service-account mode, and
  service accounts cannot own Drive files. If you switch to OAuth, "creates" becomes true.
- Don't say "Dockerized" without adding "the image isn't built on this machine".
- If something fails, narrate it. A handled failure is a better demo than a lucky pass.

---

## Before you hit record

```powershell
cd D:\Technical_Assessment
.\.venv\Scripts\Activate.ps1

# Clean slate so files appear on camera
Remove-Item workspace\*.csv, workspace\*.xlsx, workspace\*.ods -ErrorAction SilentlyContinue
Remove-Item .agent_memory\*.json -ErrorAction SilentlyContinue

python main.py doctor        # every row must be OK
```

- `.env` → `LLM_MODEL=llama-3.3-70b-versatile` (12k/min — fewer pauses than gpt-oss)
- `.env` → `EXCEL_VISIBLE=true`, `EXCEL_KEEP_OPEN=true`
- Close all Excel windows
- Browser: one tab on `localhost:8000`, one on your Google Sheet, signed in
- Arrange windows so **Excel is visible when it pops up** — don't let the browser cover it
- Terminal font large, ~110 columns wide

---

## 1 · What this is  (0:00 – 0:45)

**> DO** Show the project open in the editor. Nothing running yet.

**> SAY**

> "This is my submission for the Agentic AI Developer assessment.
>
> The task was: one natural-language command has to generate a CSV of employee data, open the
> real Microsoft Excel application, import the data, save the workbook, push the same data to
> Google Sheets, and then report what worked and what didn't.
>
> The important constraint in the brief was that this must not be a script with an LLM bolted
> on. The agent has to choose its own tools. So I'll show you the workflow first, and then I'll
> prove the tool selection is actually dynamic by taking a tool away from it."

---

## 2 · The toolbox  (0:45 – 1:30)

**> DO** `python main.py tools`

**> SAY**

> "The agent has eight tools. Generate a CSV, convert between spreadsheet formats, probe Excel,
> import into Excel, verify a workbook, upload to Google Sheets, verify a sheet, and preview a
> file.
>
> Each one is a small class with a Pydantic model describing its arguments — and that model
> *is* the JSON Schema sent to the language model. So the schema and the runtime validation
> can never drift apart, because they're the same object."

**> DO** `python main.py tools --schemas` — scroll to `generate_employee_csv`

**> SAY**

> "This is what the model actually receives. And there's a detail worth pointing at: this is
> not the raw Pydantic output. Pydantic renders an optional integer as a nullable `anyOf`
> union, and when I ran this on Llama it mis-generated against that — it produced a JSON array
> where an object belonged, and Groq rejected the whole turn.
>
> So I flatten the schema before sending it. `--raw` shows the original if you want to compare.
> That's a real interop bug I found by running it, not by guessing."

---

## 3 · Environment check  (1:30 – 2:00)

**> DO** `python main.py doctor`

**> SAY**

> "Before spending an API call, `doctor` checks the things that actually break: the provider,
> the API key, whether Excel is registered for COM automation, whether the Google credentials
> load, and whether the working directories exist.
>
> This exists so that when a run fails, I'm debugging the run — not discovering that the
> credentials were never set up. Everything is green, so let's go."

---

## 4 · The main run  (2:00 – 5:00)

**> DO** `python main.py` → browser opens on `localhost:8000`

**> SAY**

> "Running it with no arguments opens the web UI. Deliberately it does *not* start an agent
> run, because a run costs real API tokens and that shouldn't happen by accident.
>
> These badges across the top are live from a health endpoint — the provider is Groq on the
> free tier, the model, Excel COM is ready, Google Sheets is configured, eight tools enabled.
> The whole project runs at zero cost on a free key."

**> DO** Point at **Workspace files** — empty.

**> SAY**

> "Workspace is empty right now. Whatever the agent writes will show up here."

**> DO** Click the **"The assessment prompt"** chip. Then **Run agent**.

**> SAY**

> "This is the exact prompt from the assessment. Nothing else — no flags, no configuration."

### As the Plan card appears

**> SAY**

> "First thing that happens is planning. Before touching a single tool, the agent produces an
> ordered plan — and notice each step has a declared fallback. Step three says: launch Excel,
> and if that fails, fall back to writing the .xlsx directly. It also lists the risks it
> anticipates.
>
> This is a separate model call constrained to a JSON schema, and the plan gets injected into
> the system prompt for the execution phase."

### As the Activity trace fills in

**> SAY**

> "Now the execution loop. Each line is a real tool call, streamed live over Server-Sent
> Events. Cyan is a call going out, green is a success with its timing.
>
> It generated the CSV. Then — and nothing in my prompt told it to do this — it decided to
> probe whether Excel is automatable *before* committing to the Excel path."

### When Excel appears on screen — PAUSE HERE

**> DO** Let the Excel window sit on screen. Don't rush. Scroll the sheet a little.

**> SAY**

> "And there's Excel. This is the real application, launched over COM automation — not a
> library quietly writing an .xlsx file behind the scenes.
>
> It's using Excel's own text-import engine, a QueryTable, which is the same thing you'd get
> from Data → From Text in the ribbon. Bold header, frozen top row, AutoFilter arrows,
> auto-fitted columns, and the salary column is formatted as a number — so it's a real
> spreadsheet you could actually work in, not just a data dump.
>
> The step report will say `engine=excel-com`, which is how the agent proves it launched the
> application rather than falling back."

### As verify steps complete

**> SAY**

> "Now it verifies. It re-opens the workbook it just saved, from disk, and checks the header
> row and the row count. Then it does the same on the Google Sheets side — reads the sheet
> back through the API and compares.
>
> This matters. Most implementations write the data and report success. This one reports
> *verified* success, so a silent truncation would be caught instead of announced as a win."

### The Result card

**> SAY**

> "And that's the report. Every step with SUCCESS or FAILED, the timing, and concrete
> evidence — row counts, absolute file paths, the spreadsheet URL.
>
> The exit code follows too: zero for completed, one for partial, two for failed. So this drops
> straight into CI."

**> DO** Click **View data** on the `.xlsx` in **Generated files**.

**> SAY**

> "And I can see the workbook contents right here in the browser — this is reading the saved
> .xlsx back off disk and rendering it, which is another way of confirming Excel really wrote
> what it said it wrote."

**> DO** Switch to the Google Sheet browser tab. Refresh.

**> SAY**

> "Same data in Google Sheets, written through the Sheets API v4, with the header row bold and
> frozen. Same row count as the workbook."

---

## 5 · Proving it's an agent, not a script  (5:00 – 6:15)

> This is the segment the brief is really testing. Don't skip it.

**> DO** Open `config\tools.yaml`, set `excel_import_csv: enabled: false`, save.

**> SAY**

> "The brief said explicitly: don't hardcode every step into one script, the agent should
> select tools dynamically. So let me prove that.
>
> I'm disabling the Excel import tool in configuration. No code change. The agent will simply
> never see that tool exists."

**> DO** `python main.py tools` — show the ✘

**> DO** Back in the UI, same prompt, **Run agent**.

**> SAY**

> "Identical prompt. And look at the plan — it's a different shape. It's reaching for
> `convert_spreadsheet` to produce the .xlsx instead, because the Excel tool isn't in its
> toolbox any more.
>
> And critically, the report will say the Excel *application* was not launched. It doesn't
> claim success it can't evidence."

**> DO** Re-enable the tool.

**> SAY**

> "If you want a second demonstration — earlier I gave it: 'generate 30 records, import into
> Excel, also give me an ODS copy, skip Google Sheets.' It never called the Google Sheets tool
> at all, and it picked up the format-conversion tool for the ODS. Same code, different plan."

---

## 6 · Engineering underneath  (6:15 – 7:30)

**> DO** `pytest`

**> SAY**

> "285 tests, and they run fully offline — no API key, no Excel, no Google credentials — so
> this works in CI. The agent loop itself is tested against a scripted fake model, so I can
> assert on retries, on partial failures, on a hallucinated tool name, without spending tokens."

**> DO** `pytest -m slow`

**> SAY**

> "And two opt-in tests that launch the real Excel application."

**> DO** `Get-Content logs\agent-*.jsonl -Tail 3`

**> SAY**

> "Structured logging — one JSON object per line, and every line carries the run ID, so a
> single run can be grepped out of a busy log."

**> SAY** (no action needed — this is the part worth talking about)

> "The piece I'd most want to talk through is error handling. Every tool failure carries a
> `retryable` flag. Transient things — Excel busy with a dialog, an HTTP 429 — retry with
> exponential backoff. Permanent things — a missing file, missing credentials — fail on the
> *first* attempt and go straight back to the model with a remediation hint, so it can re-plan
> instead of burning three identical calls.
>
> A concrete example: my Google service account got a bare 403 permission error. Instead of
> re-throwing Google's message, I checked the account's Drive storage quota, found it was zero,
> and traced it to Google removing storage from service accounts — which means they can't own
> Drive files, so they can't create a spreadsheet. Now the agent detects that exact case and
> tells you which of the two fixes to apply. That's the difference between an integration and
> a demo."

**> DO** Briefly show `/docs`

**> SAY**

> "There's a FastAPI surface too — the UI, a REST API and the SSE stream all sit on one event
> bus. The CLI, the browser and the logs are all just subscribers; the agent doesn't know
> which one is attached. And there's an MCP server that exposes the same eight tools to Claude
> Desktop or any MCP host."

---

## 7 · Close  (7:30 – 8:00)

**> SAY**

> "So to summarise: one natural-language command, no further interaction. A CSV with realistic
> employee data, real Excel automation with a saved workbook, the same data in Google Sheets,
> both destinations verified by reading them back, and a per-step report.
>
> On top of the requirements: multi-step planning, session memory, three spreadsheet formats,
> retry logic, configurable tools, an MCP server, structured logging, live progress, a web UI,
> and 285 tests.
>
> Two things I'd flag honestly. The Docker image is written but I haven't built it — Docker
> Desktop wasn't running on this machine, and I've said so in the README rather than claim it
> works. And the agent runs on Groq's free tier, which caps tokens per minute, so you'll see
> it pause between turns; the README documents how to switch models or providers.
>
> Thanks for watching — the README has setup instructions and there's a walkthrough document
> with the reasoning behind the design decisions."

---

## If something breaks on camera

| What happens | What to say, and do |
|---|---|
| Rate limit (429/413) | "That's the free tier's per-minute token cap — notice the agent tells you exactly what to change." Then switch model in the UI dropdown and re-run. |
| `tool_use_failed` retries | "That's the model producing an unparsable tool call. My adapter re-samples up to three times — you can see it recovering." |
| Excel shows a dialog | Close it. "The retry policy exists for exactly this; you'll see the amber retry line." |
| Google 403 | "That's the service-account storage case I mentioned. The agent names the fix rather than re-throwing Google's error." |
| Run goes slow | Say why: "It's waiting on the per-minute rate limit, not thinking." |
| Total failure | `python main.py doctor` on camera. "This isolates which prerequisite broke." Have a completed run in another window as backup. |

---

## Time budget

| Segment | Minutes |
|---|---|
| 1 What this is | 0:45 |
| 2 Toolbox + schemas | 0:45 |
| 3 doctor | 0:30 |
| 4 The main run | 3:00 |
| 5 Agent-not-script | 1:15 |
| 6 Engineering | 1:15 |
| 7 Close | 0:30 |
| **Total** | **~8:00** |

Two agent runs at ~26k tokens each. On a 100k/day budget that's fine — but **don't rehearse the
full flow on the model you're recording with.** Rehearse on the other one.
