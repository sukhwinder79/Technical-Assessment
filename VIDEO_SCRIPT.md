# Video script — 5 minutes 30

Read the **READ** blocks aloud as written. **SCREEN** is what to show.

| # | Segment | Start | Length |
|---|---|---|---|
| 1 | What this is | 0:00 | 0:30 |
| 2 | Give it the instruction | 0:30 | 0:40 |
| 3 | Excel opens | 1:10 | 1:05 |
| 4 | Verified, and the report | 2:15 | 1:00 |
| 5 | It's an agent, not a script | 3:15 | 1:20 |
| 6 | Close | 4:35 | 0:40 |
| | **Total** | | **5:15** |

## Before recording

```powershell
cd D:\Technical-Assessment
.\.venv\Scripts\Activate.ps1
Remove-Item workspace\*.csv, workspace\*.xlsx, workspace\*.ods -ErrorAction SilentlyContinue
python main.py doctor
```

`.env` → `LLM_MODEL=llama-3.3-70b-versatile`, `EXCEL_VISIBLE=true`, `EXCEL_KEEP_OPEN=true`.
Close all Excel windows. Two browser tabs ready: `localhost:8000` and your Google Sheet.
Place windows so **Excel isn't hidden** when it appears.

**Never say:** "creates a Google Sheet" (say *writes into*) · "it's Dockerized" (say *Dockerfile
written, image not built*) · any row count from memory — read it off the screen.

---

# 1 · What this is — 0:00 to 0:30

**SCREEN** The web UI at `localhost:8000`.

**READ**

> This is my submission for the Agentic AI Developer assessment.
>
> One natural-language command has to generate a CSV of employee data, open Microsoft Excel,
> import it, save the workbook, put the same data in Google Sheets, and confirm both worked.
>
> The brief also said: don't hardcode the steps — the agent must choose its own tools. I'll
> prove that at the end.

---

# 2 · Give it the instruction — 0:30 to 1:10

**SCREEN** Point at the empty **Workspace files** card. Click the **"The assessment prompt"**
chip. Click **Run agent**. The Plan card appears.

**READ**

> Workspace is empty. This is the exact prompt from the assessment — nothing else.
>
> First thing it does is plan. Before touching a single tool, it writes an ordered plan, and
> each step has a declared fallback — step three says launch Excel, and if that fails, write
> the file directly instead. It also lists the risks it expects.
>
> Now the execution loop. Every green line is a real tool call, streamed live.

---

# 3 · Excel opens — 1:10 to 2:15

**SCREEN** Excel appears. **STOP TALKING FOR THREE SECONDS.** Let it sit. Scroll slightly.

**READ**

> And there's Excel.
>
> This is the real application, launched over COM automation — not a library quietly writing a
> spreadsheet file. It's using Excel's own text-import engine, the same thing you'd get from
> Data, From Text in the ribbon.
>
> Bold header, frozen top row, AutoFilter, and salary formatted as a number.
>
> The report says engine equals excel-com. That's how the agent evidences it launched the
> application instead of falling back. If Excel were missing it would say so, not pretend.

---

# 4 · Verified, and the report — 2:15 to 3:15

**SCREEN** The verify steps, then the Result card. Click **View data** on the `.xlsx`. Then
switch to the Google Sheet tab and refresh.

**READ**

> Then it verifies. It re-opens the workbook from disk and checks the header row and row count,
> and does the same on the Google Sheets side through the API.
>
> Most implementations write data and report success. This reports *verified* success — so a
> silent truncation gets caught instead of being announced as a win.
>
> Here's the report. Every step with its status, timing, and evidence. Exit code zero for
> completed, one for partial, two for failed — so it fits a CI pipeline.
>
> I can view the saved workbook right in the browser. And here's the same data in Google
> Sheets, written through the Sheets API.

---

# 5 · It's an agent, not a script — 3:15 to 4:35

> The segment the brief is really testing. Don't skip it.

**SCREEN** Open `config\tools.yaml`. Set `excel_import_csv: enabled: false`. Save. Back in the
UI, same prompt, **Run agent**. **Stop the run once the plan renders** — that's the whole point.

**READ**

> The brief said the agent should select tools dynamically, so let me prove it rather than
> claim it.
>
> I'm disabling the Excel import tool in a config file. No code change. The agent simply never
> sees that this tool exists.
>
> Same prompt. And the plan comes out a different shape — it's reaching for the format
> conversion tool instead, and the report will say the Excel application was not launched.
>
> One more from earlier: I asked it for thirty records, Excel, an ODS copy, and skip Google
> Sheets. It never called the Sheets tool at all, and the plan came out five steps instead of
> six.

**SCREEN** Re-enable the tool.

---

# 6 · Close — 4:35 to 5:15

**READ**

> So: one command, no further interaction. CSV, real Excel with a saved workbook, the same data
> in Google Sheets, both verified by reading them back, and a per-step report.
>
> Beyond the requirements it also has multi-step planning, session memory, three spreadsheet
> formats, retry logic, configurable tools, an MCP server, structured logging, and two hundred
> and ninety tests that run offline.
>
> One honest caveat — the Dockerfile is written but I haven't built the image, and the README
> says so rather than claiming it works.
>
> Thanks for watching. Setup is in the README.

---

## If it breaks, narrate it

A handled failure demonstrates the error handling better than a lucky pass.

| Problem | Say this |
|---|---|
| Rate limit (429 / 413) | "That's the free tier's per-minute token cap — notice the agent tells you what to change." Switch model in the UI dropdown, re-run. |
| Long pause mid-run | "It's waiting on the rate limit, not thinking." |
| Anything else | Run `python main.py doctor` on camera: "this isolates which prerequisite broke." |

Keep a completed run open in a second window as a fallback.
