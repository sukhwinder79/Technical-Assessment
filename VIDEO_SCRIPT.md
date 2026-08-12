# Video script — read aloud

Everything in a **READ** block is meant to be spoken as written. Roughly 140 words a minute,
which is what the timings assume. **SCREEN** tells you what to have visible.

Each segment names the assessment requirement it satisfies, so nothing is demonstrated
without saying why it's there.

Timings are **measured, not guessed**: every READ block was word-counted and divided by 140
wpm, then given room for the command to actually run. Total is **9:45** — inside the 5–10
minute requirement with a minute spare for a stumble. If you need it shorter, drop segment 2's
second half and shorten segment 11; that's 90 seconds and costs nothing structural.

| # | Segment | Start | Length | Speech | Rest of the slot |
|---|---|---|---|---|---|
| 1 | What was asked | 0:00 | 0:50 | 48s | — |
| 2 | The toolbox | 0:50 | 0:55 | 49s | typing two commands |
| 3 | Preflight check | 1:45 | 0:25 | 21s | — |
| 4 | Give it the instruction | 2:10 | 0:30 | 28s | — |
| 5 | Planning | 2:40 | 0:40 | 35s | plan renders ~15s in |
| 6 | Excel — the main event | 3:20 | 1:25 | 73s | 3s deliberate silence + Excel launch |
| 7 | Verification | 4:45 | 0:45 | 42s | — |
| 8 | The report | 5:30 | 0:45 | 36s | clicking **View data** |
| 9 | Google Sheets | 6:15 | 0:35 | 27s | tab switch + refresh |
| 10 | Proof it's an agent | 6:50 | 1:10 | 60s | editing the YAML |
| 11 | Engineering underneath | 8:00 | 1:00 | 54s | pytest takes ~20s |
| 12 | Close | 9:00 | 0:45 | 45s | — |
| | **Total** | | **9:45** | **8:38** | 67s of headroom |

> **Segment 10 does not need a full second run.** The point is that the *plan* comes out a
> different shape, and that appears about fifteen seconds in. Narrate it, then stop the run —
> that saves ninety seconds of dead air and 26,000 tokens.

---

## Before you press record

```powershell
cd D:\Technical-Assessment
.\.venv\Scripts\Activate.ps1
Remove-Item workspace\*.csv, workspace\*.xlsx, workspace\*.ods -ErrorAction SilentlyContinue
python main.py doctor
```

- `.env` → `LLM_MODEL=llama-3.3-70b-versatile`, `EXCEL_VISIBLE=true`, `EXCEL_KEEP_OPEN=true`
- Close every Excel window
- Two browser tabs: `localhost:8000`, and your Google Sheet (signed in)
- Position windows so Excel is **not** hidden by the browser when it appears

**Three things never to say:** "creates a Google Sheet" (say *writes into*), "it's Dockerized"
(say *the Dockerfile is written but I haven't built the image*), and any row count from memory
— read it off the screen.

---

# 1 · What was asked — 0:00 to 0:50

**SCREEN** The project open in your editor. Nothing running.

**READ** *(105 words)*

> Hi. This is my submission for the Agentic AI Developer assessment.
>
> The brief asked for an autonomous agent that completes a real task using tools, rather than
> just answering as a language model.
>
> One command has to do six things. Generate a CSV of employee data. Open Microsoft Excel.
> Import the data. Save the workbook. Put the same data into Google Sheets. And then confirm
> both imports actually worked.
>
> The brief was also explicit about what it did *not* want. It said, don't hardcode every step
> into one script — the agent should select and invoke tools dynamically.
>
> So I'll show the workflow first, and then prove the tool selection is genuinely dynamic.

---

# 2 · The toolbox — 0:50 to 1:45

**SCREEN** `python main.py tools`

**READ** *(50 words)*

> The agent has eight tools — generate a CSV, convert formats, probe Excel, import into Excel,
> verify a workbook, upload to Sheets, verify a sheet, preview a file.
>
> Each one is a class with a Pydantic model for its arguments. That model *is* the JSON schema
> sent to the model, so schema and validation can't drift apart.

**SCREEN** `python main.py tools --schemas` — scroll to `generate_employee_csv`

**READ** *(55 words)*

> This is what the model actually receives — and it's not the raw Pydantic output.
>
> Pydantic renders an optional integer as a nullable "anyOf" union. Llama mis-generated against
> that: it produced an array where an object belonged, and Groq rejected the whole turn. So I
> flatten the schema first. A real bug I found by running it, not guessing.

---

# 3 · Preflight check — 1:45 to 2:10

**SCREEN** `python main.py doctor`

**READ** *(50 words)*

> Before spending a single API call, this checks what actually breaks. The provider. The API
> key. Whether Excel is registered for COM automation. Whether the Google credentials load.
> And whether the working directories exist.
>
> So when a run fails, I'm debugging the run — not discovering that setup was wrong.

---

# 4 · Give it the instruction — 2:10 to 2:40

> **Requirement: accept natural language input**

**SCREEN** `python main.py` → browser opens at `localhost:8000`

**READ** *(45 words)*

> No arguments opens the web UI — deliberately, it does not start a run, because a run costs
> real tokens.
>
> These badges are live from a health endpoint. Groq on the free tier, the model, Excel COM
> ready, Sheets configured, eight tools. The whole thing runs at zero cost.

**SCREEN** Point at the empty **Workspace files** card. Click the **"The assessment prompt"**
chip. Then click **Run agent**.

**READ** *(18 words)*

> Workspace is empty. This is the exact prompt from the assessment — no flags, nothing else.
> And run.

---

# 5 · Planning — 2:40 to 3:20

> **Requirement (bonus): multi-step planning before execution**

**SCREEN** The Plan card as it appears.

**READ** *(85 words)*

> First thing that happens is planning.
>
> Before touching a single tool, the agent writes an ordered plan. And notice each step has a
> declared fallback. Step three says: launch Excel — and if that fails, fall back to writing
> the spreadsheet file directly.
>
> It also lists the risks it anticipates. Excel might not be installed. Google credentials
> might be missing.
>
> This is a separate model call, constrained to a JSON schema. The plan then gets injected
> into the prompt for the execution phase.

---

# 6 · Excel — the main event — 3:20 to 4:45

> **Requirements: launch Excel · import the CSV · save the workbook**

**SCREEN** The Activity trace filling in.

**READ** *(70 words)*

> Now the execution loop. Every line is a real tool call, streamed live over server-sent
> events. Cyan is a call going out. Green is a success, with its timing.
>
> It generated the CSV. And then — nothing in my prompt told it to do this — it decided to
> check whether Excel is automatable *before* committing to the Excel path. That's the agent
> reasoning, not a script running.

**SCREEN** Excel window appears. **STOP TALKING FOR THREE SECONDS.** Let it sit. Scroll it
slightly.

**READ** *(115 words)*

> And there's Excel.
>
> This is the real application, launched through COM automation. It is not a library quietly
> writing a spreadsheet file in the background.
>
> It's using Excel's own text-import engine — a QueryTable — which is exactly what you'd get
> from Data, From Text in the ribbon.
>
> Bold header row. Frozen top row. AutoFilter arrows. Auto-fitted columns. And the salary
> column is formatted as a number, so you could actually sum it.
>
> The step report will say engine equals excel-com. That's how the agent evidences that it
> launched the application, rather than silently falling back. If Excel were missing, it would
> say so instead of pretending.

---

# 7 · Verification — 4:45 to 5:30

> **Requirement: confirm that both imports completed successfully**

**SCREEN** The two verify steps completing.

**READ** *(95 words)*

> Now it verifies, and this is the part I'd most want you to notice.
>
> It re-opens the workbook it just saved, from disk, and checks the header row and the row
> count. Then it does the same on the Google Sheets side — reads the sheet back through the
> API and compares.
>
> Most implementations write the data and report success. This one reports *verified* success.
> So a silent truncation would be caught, instead of being announced as a win.
>
> And I tested that the verifier can fail. Give it wrong expectations and it returns
> "verified: false" with the reasons.

---

# 8 · The report — 5:30 to 6:15

> **Requirement: report whether each step succeeded or failed**

**SCREEN** The Result card. Then click **View data** on the `.xlsx`.

**READ** *(85 words)*

> And there's the report. Every step, with success or failed, the timing, and concrete
> evidence — row counts, absolute file paths, the spreadsheet URL.
>
> The exit code follows the same logic. Zero for completed, one for partial, two for failed.
> So this drops straight into a CI pipeline.
>
> And I can view the workbook contents right here in the browser. This is reading the saved
> file back off disk and rendering it — which is one more way of confirming Excel wrote what
> it said it wrote.

---

# 9 · Google Sheets — 6:15 to 6:50

> **Requirements: connect via the Google Sheets API · import the same CSV**

**SCREEN** Switch to the Google Sheet tab. Refresh.

**READ** *(60 words)*

> And the same data in Google Sheets, written through the Sheets API version four, with the
> header row bold and frozen. Same row count as the workbook.
>
> One honest note here. I'm authenticating as a service account, and service accounts have no
> Drive storage, so they can't own a file. It writes into a sheet I own. In OAuth mode it
> creates them itself.

---

# 10 · Proof it's an agent — 6:50 to 8:00

> **Restriction: "avoid hardcoding every step into one script"** — this is the segment the
> brief is really testing. Do not skip it.

**SCREEN** Open `config\tools.yaml`. Set `excel_import_csv: enabled: false`. Save.
Then `python main.py tools` to show the ✘.

**READ** *(65 words)*

> The brief said: don't hardcode the steps, the agent should select tools dynamically. So let
> me prove that rather than claim it.
>
> I'm disabling the Excel import tool in a configuration file. No code change at all. The
> agent will simply never see that this tool exists.

**SCREEN** Back in the UI. Same prompt. **Run agent**.

**READ** *(85 words)*

> Identical prompt. And look at the plan — it's a different shape.
>
> It's reaching for the format-conversion tool to produce the spreadsheet instead, because the
> Excel tool isn't in its toolbox any more. And the report says the Excel *application* was
> not launched. It doesn't claim a success it can't evidence.
>
> One more example from earlier. I gave it: generate thirty records, import into Excel, also
> give me an ODS copy, skip Google Sheets. It never called the Google Sheets tool at all, and
> the plan came out five steps instead of six.

**SCREEN** Re-enable the tool.

---

# 11 · Engineering underneath — 8:00 to 9:00

**SCREEN** `pytest`

**READ** *(45 words)*

> Two hundred and ninety tests, running completely offline — no API key, no Excel, no Google
> credentials, so this works in CI.
>
> The agent loop is tested against a scripted fake model, which lets me assert on retries,
> partial failures, and a hallucinated tool name, without spending tokens.

**SCREEN** `Get-Content logs\agent-*.jsonl -Tail 3`

**READ** *(75 words)*

> Structured logging — one JSON object per line, each carrying the run ID.
>
> The part I'd most want to discuss is error handling. Every failure carries a "retryable"
> flag. Transient things retry with backoff. Permanent things — missing file, missing
> credentials — fail on the first attempt and go back to the model with a remediation hint.
>
> For example, my service account returned a bare 403. Instead of re-throwing it, I check the
> Drive storage quota, find it's zero, and name the fix.

---

# 12 · Close — 9:00 to 9:45

**SCREEN** The finished report, or the README.

**READ** *(95 words)*

> To summarise. One command, no further interaction. A CSV of realistic employee data, real
> Excel automation with a saved workbook, the same data in Google Sheets, both destinations
> verified by reading them back, and a per-step report.
>
> Beyond the requirements: planning, session memory, three formats, retry logic, configurable
> tools, an MCP server, structured logging, a web UI, and two hundred and ninety tests.
>
> One honest caveat — the Dockerfile is written but I haven't built the image, and the README
> says so rather than claiming it works.
>
> Thanks for watching. Setup is in the README, and there's a walkthrough with the reasoning
> behind the design decisions.

---

## Requirement coverage — what you said, and where

Use this if the interviewer asks you to point at something specific.

| The brief asked | Covered in segment |
|---|---|
| Accept natural language input | 4 |
| Decide which tools to execute | 6, proved in 10 |
| Generate a CSV automatically | 6 |
| At least 20 rows of realistic data | 6 (read the count off screen) |
| Launch Microsoft Excel | 6 |
| Import the CSV into Excel | 6 |
| Save the workbook | 6, 8 |
| Connect to Google Sheets via the API | 9 |
| Import the same CSV into the sheet | 9 |
| Report each step's success/failure | 8 |
| Handle errors gracefully | 11 |
| Multi-step planning (bonus) | 5 |
| Memory / history (bonus) | 12 (mentioned) |
| XLSX / CSV / ODS (bonus) | 10 |
| Retry logic (bonus) | 11 |
| Configurable tools (bonus) | 10 |
| MCP server (bonus) | 12 |
| Docker (bonus) | 12, with the caveat |
| Unit tests (bonus) | 11 |
| Structured logging (bonus) | 11 |
| Progress updates (bonus) | 6 |
| **Not just a script** | **10** |

---

## If it breaks while recording

Narrate it. A handled failure demonstrates the error handling better than a lucky pass.

| What happens | Say this |
|---|---|
| Rate limit (429 / 413) | "That's the free tier's per-minute token cap — notice the agent tells you exactly what to change." Then switch model in the UI dropdown and re-run. |
| `tool_use_failed` retries | "That's the model emitting an unparsable tool call. My adapter re-samples up to three times — you can see it recovering." |
| Long pause mid-run | "It's waiting on the rate limit, not thinking." |
| Excel shows a dialog | Close it. "The retry policy exists for this — you'll see the amber retry line." |
| Google 403 | "That's the service-account storage case I mentioned. The agent names the fix rather than re-throwing Google's error." |
| Everything fails | Run `python main.py doctor` on camera. "This isolates which prerequisite broke." |

Keep a completed run open in a second window as a fallback.

**Token budget:** two runs at roughly 26,000 tokens each. The free tier allows 100,000 a day
per model — so rehearse on `openai/gpt-oss-120b` and record on `llama-3.3-70b-versatile`.
