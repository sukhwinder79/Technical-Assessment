# Demo video script (5–10 minutes)

A running order for the required demo recording. Timings are a guide; the whole thing
fits in about eight minutes at a normal pace.

There are two ways to run the main demo — the **web UI** (§3a) reads better on video, the
**CLI** (§3b) proves there is no hidden state. Do the UI as the headline and the CLI as the
"same thing, one command" follow-up; §4 onward is CLI either way.

## Before you hit record

```powershell
cd D:\Technical_Assessment
.\.venv\Scripts\Activate.ps1

agentic-sheets doctor          # everything must be OK
Remove-Item workspace\* -Force -ErrorAction SilentlyContinue
Remove-Item .agent_memory\* -Force -ErrorAction SilentlyContinue
```

Checklist:

- `.env` has `LLM_PROVIDER=groq` and a valid `GROQ_API_KEY` (free — mention on camera that
  the whole thing runs at zero cost, and that `--provider anthropic` swaps in Claude)
- **`LLM_MODEL=openai/gpt-oss-120b`.** Verified strongest of the free Groq models at
  multi-step tool calling. `llama-3.3-70b-versatile` also works; `llama-3.1-8b-instant`
  does **not** — it cannot emit a parsable tool call. Do not discover that on camera.
- `EXCEL_VISIBLE=true` and `EXCEL_KEEP_OPEN=true` — the Excel window appearing on screen
  is the single most convincing moment in the video
- Google Sheets: in `service_account` mode, `GOOGLE_SPREADSHEET_ID` must point at a sheet
  you own and have shared with the service account as Editor. **Say "writes into a Google
  Sheet", not "creates one"** — service accounts have no Drive storage and cannot create
  files. (In `oauth` mode it does create them, and then you can say "creates".)
- Open that spreadsheet in a browser tab beforehand, signed in, so the URL opens instantly
- Close any open Excel windows
- Terminal font size up; window wide enough (~110 columns) that the tables don't wrap

### Token budget — plan the recording around it

Groq's free tier allows **100,000 tokens per day, metered per model**. One full six-step run
costs **~26,000**. So:

| Runs in the video | Tokens | Safe? |
|---|---|---|
| 1 full run (§3a or §3b) | ~26k | yes |
| + the tool-disabled re-run (§5) | ~52k | yes |
| + a UI run | ~78k | tight — prefer showing the UI without pressing Run |

Rehearse with `--no-plan` (one fewer request) or on a different model, since each model has
its own budget. If you do exhaust one, `--model llama-3.3-70b-versatile` is a live spare.
Do **not** rehearse the full flow repeatedly on the model you plan to record with.

---

## 1 · Frame the problem (0:00–0:45)

Show `README.md` briefly, then say what this is:

> "One natural-language command has to produce a CSV, drive the real Excel application,
> save a workbook, upload the same data to Google Sheets, and report what worked. The
> model decides which tools to call — there is no fixed pipeline."

Show the toolbox so the viewer knows what the agent has to work with:

```powershell
agentic-sheets tools
```

Point out that eight tools are registered and that `config/tools.yaml` controls which ones
the agent can see.

---

## 2 · The environment check (0:45–1:15)

```powershell
agentic-sheets doctor
```

Call out three rows: the LLM provider (`groq (free tier)` — no cost to run this), Excel
(`16.0` registered for COM automation), and Google (credentials load, service-account
mode). This makes it clear the later success isn't faked.

---

## 3a · The main event, in the browser (1:15–3:30)

```powershell
agentic-sheets serve --open
```

The page opens on <http://localhost:8000>. Point at the header chips first — provider
`groq (free tier)`, the model, `excel COM ready`, `8 tools` — all read live from `/health`.

Click the first example chip to fill the box, then **Run agent**. Narrate as the cards fill
in, top to bottom:

1. **Plan card** — an ordered plan with a declared fallback per step, written *before* any
   tool ran.
2. **Activity card** — the live trace, streamed over Server-Sent Events. Tool calls in cyan,
   successes green, retries amber, failures red. Also the model's own narration explaining
   what it's about to do.
3. **Excel appears on screen** mid-run. Pause here — this is the moment. Let the viewer see
   the window, the imported rows, the bold header, the frozen pane, the AutoFilter arrows.
4. **Result card** — per-step SUCCESS/FAILED with timings, then the artifacts, with the
   Google Sheets URL as a live link. **Click it on camera** and show the same 25 rows.

Worth saying out loud: the browser is just another subscriber to the same event bus the CLI
renders — the agent has no idea a UI is attached.

Optional 15-second flourish: change **Provider** to `anthropic` and re-run the same prompt.
Same agent, same tools, different model.

---

## 3b · The same thing from one command (3:30–4:00)

```powershell
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets."
```

Show that the terminal renders the identical plan → trace → step-report, and that the exit
code is meaningful (`0` completed, `1` partial, `2` failed) so it drops into CI:

```powershell
echo $LASTEXITCODE
```

Narrate as it happens:

1. **The plan panel** — the agent wrote an ordered plan with a fallback per step *before*
   touching anything. That's the multi-step planning requirement.
2. **`generate_employee_csv`** — read the row count off the trace rather than saying a
   number from memory; the agent chooses it (20 in one run, 50 in another — the brief only
   sets a floor of 20). That it chose is the point worth making.
3. **Excel launches on screen** — pause here. Let the viewer see the window, the imported
   data, the bold header, the frozen pane, the AutoFilter arrows. Mention this is Excel's
   own *Data → From Text* import via a `QueryTable`, not a library writing a file.
4. **`excel_verify_workbook`** — the agent re-opens the saved file from disk to confirm the
   row count. It reports verified success, not assumed success.
5. **`google_sheets_import`** — a live Sheets API call; a URL comes back.
6. **The step report** — per-step SUCCESS/FAILED with evidence, the artifacts panel, and
   the agent's own written report.

Then open the artifacts. The agent names the file itself, so take the path from the
**Artifacts** panel rather than typing one you remember:

```powershell
explorer workspace            # then double-click the .xlsx the run just reported
echo $LASTEXITCODE            # 0 = completed · 1 = partial · 2 = failed — usable in CI
```

…and switch to the spreadsheet tab. Show the same row count in both.

---

## 4 · It is an agent, not a script (4:00–5:30)

This is the most important segment. Disable a tool and re-run the *identical* prompt.

```powershell
notepad config\tools.yaml
```

Set:

```yaml
  excel_import_csv:
    enabled: false
```

Then:

```powershell
agentic-sheets tools          # excel_import_csv now shows ✘
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets."
```

Point out that the plan is now a **different shape** — the agent reaches for
`convert_spreadsheet(target_format='xlsx')` instead, and its report states plainly that
the Excel *application* was not launched. Nothing in the prompt changed and no code changed.

Re-enable the tool before continuing.

---

## 5 · Memory across runs (5:30–6:30)

> **Token warning.** Doing this live costs two more runs (~52k). If you have already spent
> the budget on §3 and §4, record it in a separate take on another day, or simply show a
> stored session — `agentic-sheets sessions --show demo` is free and proves the same point
> with the persisted `last_csv_path` visible in the JSON.

```powershell
agentic-sheets run "Generate 40 employee records and load them into Excel" --session demo
agentic-sheets run "Now export that same data to ODS as well" --session demo --continue
```

The second run does **not** regenerate the CSV — it resolves "that same data" from working
memory. Show it:

```powershell
agentic-sheets sessions --show demo
```

Point at `last_csv_path` / `last_workbook_path` in the stored facts.

---

## 6 · Error handling (6:30–7:15)

Pick one — the missing-credentials case is the cleanest:

```powershell
Rename-Item credentials\service_account.json credentials\service_account.json.bak
agentic-sheets run "Create an employee CSV and upload it to Google Sheets."
```

Point out three things:

- The tool failed **once**, not three times — a missing file is not retryable, so the agent
  doesn't waste attempts.
- The error came back to the model with a `remediation` hint, which the agent surfaced in
  its report instead of guessing.
- The run is `PARTIAL`, not `FAILED`: the CSV step still succeeded, and the exit code
  reflects it.

```powershell
Rename-Item credentials\service_account.json.bak credentials\service_account.json
```

---

## 7 · Engineering surfaces (7:15–8:00)

Move quickly here — one command each.

```powershell
pytest                                  # 222 tests, fully offline
```

Provider portability in one line — the same prompt on a different LLM:

```powershell
agentic-sheets run "Create an employee CSV and import it into Excel" --provider groq --model openai/gpt-oss-120b
```

The REST surface behind the UI you already showed:

```powershell
agentic-sheets serve
```

Open <http://localhost:8000/docs>, then in a second terminal show the raw SSE stream the
browser consumes:

```bash
curl -N localhost:8000/runs/<id>/events
```

Then the structured logs:

```powershell
Get-Content logs\agent-*.jsonl -Tail 5
```

Mention in one sentence each: the MCP server (`agentic-sheets mcp`) exposes the same tools
to Claude Desktop, and `docker compose up` runs the UI and API — with the honest caveat that
Excel can't run in a Linux container, so the containerised agent uses the fallback engine
and says so.

---

## 8 · Close (8:00–8:30)

Summarise against the brief:

> "One command, no further interaction. CSV with 25 rows of realistic data, real Excel
> automation, saved workbook, Google Sheets upload, both verified, per-step reporting —
> running on a free Groq key. On top of that: planning, memory, retries, configurable
> tools, MCP, Docker, structured logging, a live web UI, and 229 tests."

Then name the limitations out loud — real Excel automation needs Windows, and Gemini would
need a third adapter. Being straight about the edges reads better than pretending there
aren't any.

---

## Recording notes

- **Don't cut the Excel launch.** It takes about four seconds; that pause is the proof.
- For the UI segment, arrange the browser and the desktop so the Excel window is visible
  when it appears — a browser maximised over it hides the best moment.
- If a run is slow, mention `--effort low` and `--no-plan` exist rather than editing in a
  jump cut.
- Keep `--quiet` off: the model's streamed narration explains its own reasoning better than
  you can talk over it.
- Have a completed run in a second terminal as a fallback in case of a live API hiccup.
