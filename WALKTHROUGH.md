# Walkthrough — run it locally, and explain it

Two things in one document:

* **Part 1** — the exact commands, in order, to set up and exercise the whole flow.
* **Part 2** — what each step *proves*, and how to explain the design in an interview.

Every command below is copy-pasteable PowerShell from the project root.

---

# Part 1 — Run it locally

## 1.1 One-time setup

```powershell
cd D:\Technical_Assessment

python -m venv .venv
.\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
pip install -e .                      # gives you the `agentic-sheets` command

copy .env.example .env
```

Now edit `.env` and set two lines:

```ini
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
```

Get that key from <https://console.groq.com/keys> — free, no credit card, ~30 seconds.

For the Google Sheets half, follow README → *Google Sheets setup* (service account, enable
**both** the Sheets API and the Drive API, save the JSON to
`credentials/service_account.json`, set `GOOGLE_SHARE_WITH_EMAIL` to your own Gmail).

> Want to try it before doing the Google setup? Set `GOOGLE_AUTH_MODE=disabled`. The Excel
> half runs and the agent reports the Sheets step as skipped — which is itself a good thing
> to show.

## 1.1a Read this before you demo on the free tier

Groq's free tier caps **tokens per minute** (12,000 on `llama-3.3-70b-versatile`). A single
six-turn run costs about **20k input tokens in total**, because an agent resends the whole
conversation each turn. So:

> **Leave about a minute between runs.** Two back-to-back runs will get the second one
> rejected, and that looks like a failure on camera when it isn't.

If you do get rejected mid-demo, the agent prints exactly what to do. The graceful recovery
is to **resume rather than restart** — earlier steps are in memory:

```powershell
agentic-sheets run "<same prompt>" --session demo --continue
```

Faster/cheaper options: `--no-plan` (removes one whole request), or
`--model openai/gpt-oss-120b`. Or `--provider anthropic` if you have that key.

## 1.2 Check the environment before running anything

```powershell
agentic-sheets doctor
```

Every row must be `OK` (Google may be `WARN` if you deliberately disabled it). This checks
the provider, the API key, Excel COM registration, Google credentials and the writable
paths **independently**, so you never debug a failed run when the real problem was setup.

## 1.3 The main flow — the assessment prompt

Close any open Excel windows first, then:

```powershell
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets."
```

Watch for, in order: the **plan** panel → tool calls in the live trace → **Excel opening on
screen** → the verify steps → the **step report** table → artifacts → the agent's written
report.

This is what a fully verified run looks like — actual output from this machine, Groq free tier,
both destinations live:

```
· Thinking (turn 1/25)…
  → generate_employee_csv(email_domain=example.com, filename=employee_sample.csv)
  ✔ generate_employee_csv → 50 rows → employee_sample.csv (0.0s)
· Thinking (turn 2/25)…
  ✔ excel_probe → Excel available (v16) (0.03s)
· Thinking (turn 3/25)…
  → Launching Microsoft Excel and importing employee_sample.csv…
  ✔ excel_import_csv → 50 rows · employee_sample.xlsx · engine=excel-com (5.52s)
· Thinking (turn 4/25)…
  ✔ excel_verify_workbook → 50 rows · employee_sample.xlsx · verified (0.24s)
· Thinking (turn 5/25)…
  → Connecting to the Google Sheets API…
  → Uploading 50 rows to Google Sheets…
  ✔ google_sheets_import → 50 rows — https://docs.google.com/spreadsheets/d/1IM…
· Thinking (turn 6/25)…
  ✔ google_sheets_verify → verified 50 rows — https://docs.google.com/spreadsheets/…

COMPLETED  6/6 steps ok · 7 model turns · 177.96s · 25822 in / 966 out tokens
```

The model chose 50 rows — nothing asked it to; the brief only sets a floor of 20. It also
chose to probe Excel before committing, which no instruction told it to do.

And this is the same prompt with `GOOGLE_AUTH_MODE=disabled`, to show honest partial reporting:

```
· Thinking (turn 1/25)…
  → generate_employee_csv(filename=sample_employees.csv, include_extended_colu…
  ✔ generate_employee_csv → 20 rows → sample_employees.csv (0.0s)
· Thinking (turn 2/25)…
  → excel_probe()
  ✔ excel_probe → Excel available (v16) (0.03s)
· Thinking (turn 3/25)…
  → excel_import_csv(apply_formatting=True, csv_path=D:\…\sample_employees.csv)
  → Launching Microsoft Excel and importing sample_employees.csv…
  ✔ excel_import_csv → 20 rows · sample_employees.xlsx · engine=excel-com (5.73s)
· Thinking (turn 4/25)…
  → excel_verify_workbook(expected_columns=['Employee ID'…'Performance Rating'])
  ✔ excel_verify_workbook → 20 rows · sample_employees.xlsx · verified (0.36s)
· Thinking (turn 5/25)…
  → google_sheets_import(csv_path=…)
  ✘ Google Sheets integration is disabled (GOOGLE_AUTH_MODE=disabled).

PARTIAL  4/5 steps ok · 6 model turns · 75.8s · 21637 in / 853 out tokens
```

Three things to notice, because they're the points worth making out loud:

* **`engine=excel-com`** — the Excel *application* was driven, not a library writing a file.
* **`verified`** — the workbook was re-opened from disk and its rows and headers checked.
* **`PARTIAL`, not `FAILED`** — the Google step is honestly reported as unavailable with the
  fix, and exit code 1 reflects partial success. The agent chose to call `excel_probe` first,
  which nothing in the prompt told it to do.

Then open the outputs:

```powershell
explorer workspace                    # employees.csv + employees.xlsx
```

…and paste the `last_spreadsheet_url` from the artifacts panel into a browser.

Check the exit code — it is meaningful, which is what makes this usable in CI:

```powershell
echo $LASTEXITCODE                    # 0 = completed · 1 = partial · 2 = failed
```

## 1.4 The same flow in the browser

```powershell
agentic-sheets serve --open
```

Click an example chip → **Run agent**. Same plan, same live trace, same report — the browser
is just another subscriber to the same event stream. The Google Sheets URL is a real link.

Change **Provider** to `anthropic` (if you have that key) and re-run the identical prompt to
show the agent is not tied to one model.

## 1.5 Prove it's an agent, not a script

This is the single most important demonstration, because the brief explicitly warns against
"hardcoding every step into one script".

**Experiment A — take a tool away.** Edit `config/tools.yaml`:

```yaml
  excel_import_csv:
    enabled: false
```

```powershell
agentic-sheets tools              # excel_import_csv now shows ✘
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets."
```

The **plan comes out a different shape** — the agent reaches for
`convert_spreadsheet(target_format='xlsx')` instead, and its report says plainly that the
Excel *application* was not launched. Same prompt, no code change.

**Re-enable it afterwards.**

**Experiment B — change the goal, not the code.**

```powershell
agentic-sheets run "I need sample HR data in three formats - CSV, XLSX and ODS - plus a Google Sheet. Verify every destination and tell me the row counts."
```

Different tool sequence, more steps, because the model planned for the new goal.

**Experiment C — narrow the scope.**

```powershell
agentic-sheets run "Build a 25-row employee dataset, import it into Excel only (skip Google Sheets), and confirm the workbook has the right headers."
```

`google_sheets_import` is never called at all.

## 1.6 Prove the bonus features

**Memory across runs**

```powershell
agentic-sheets run "Generate 40 employee records and load them into Excel" --session demo
agentic-sheets run "Now export that same data to ODS as well" --session demo --continue
agentic-sheets sessions --show demo
```

The second run does **not** regenerate the CSV — it resolves "that same data" from stored
working memory (`last_csv_path`). Point at that key in the session JSON.

**Interactive memory**

```powershell
agentic-sheets chat --session demo
```

```
you › create 20 employee records and put them in Excel
you › how many rows did that have?      <- answered from memory, zero tool calls
you › now also push it to Google Sheets
you › exit
```

**Retry vs fail-fast**

```powershell
Rename-Item credentials\service_account.json credentials\service_account.json.bak
agentic-sheets run "Create an employee CSV and upload it to Google Sheets."
Rename-Item credentials\service_account.json.bak credentials\service_account.json
```

The tool fails **once**, not three times — a missing file is not retryable. The model gets a
`remediation` hint and surfaces it instead of looping. Status is `PARTIAL`, not `FAILED`,
because the CSV step still succeeded.

**Tests**

```powershell
pytest                       # 229 tests, fully offline — no key, no Excel, no Google
pytest -m slow               # + 2 tests that launch the real Excel application
pytest --cov=agentic_sheets  # coverage
```

**Structured logging**

```powershell
Get-Content logs\agent-*.jsonl -Tail 5
```

One JSON object per line, every line carrying the `run_id`.

**Machine-readable report**

```powershell
agentic-sheets run "Create an employee CSV and import it into Excel" --json-out reports\run.json
Get-Content reports\run.json | ConvertFrom-Json | Select-Object status, duration_s
```

**HTTP API + live event stream**

```powershell
agentic-sheets serve
```

In a second terminal:

```powershell
curl.exe -s -X POST localhost:8000/runs -H "content-type: application/json" -d "{\"instruction\":\"Create an employee CSV and import it into Excel\"}"
curl.exe -N localhost:8000/runs/<run_id>/events
```

**MCP server**

```powershell
agentic-sheets mcp
```

It waits on stdio (that is correct — an MCP host drives it). Prove it works without a host:

```powershell
pytest tests\test_mcp_server.py -v
```

That test spawns the server as a subprocess and does a real protocol handshake.

**Docker**

```powershell
docker compose up --build             # UI + API on http://localhost:8000
docker compose run --rm agent-cli doctor
```

Say the caveat before they ask: Excel is a Windows desktop app and cannot run in a Linux
container, so inside Docker the agent uses the `openpyxl` fallback and reports
`excel_launched: false`.

## 1.7 Full-flow checklist

Tick these off against the brief's own wording:

| The brief asks for | Command / where you see it |
|---|---|
| Accept natural language instruction | `agentic-sheets run "<anything>"` |
| Agent decides which tools to use | The plan panel; §1.5 proves it re-plans |
| Generate a CSV automatically | `generate_employee_csv` step |
| At least 20 rows of realistic data | 25 by default, 10 columns — open the CSV |
| Open Microsoft Excel | The Excel window appearing on screen |
| Import the CSV into Excel | `engine=excel-com` in the step report |
| Save the workbook | `workspace\employees.xlsx` |
| Connect to Google Sheets via the API | `google_sheets_import` step |
| Import the same CSV into a Google Sheet | The spreadsheet URL in the artifacts |
| Confirm both imports | The two `*_verify` steps, `verified` in their detail |
| Report success/failure per step | The step report table + exit code |
| Handle errors gracefully | §1.6 retry experiment |

---

# Part 2 — How to explain it

## 2.1 The 60-second architecture answer

> "It's a tool-calling agent, not a pipeline. Three layers.
>
> **First, tools.** Eight of them — generate a CSV, convert formats, launch Excel over COM,
> verify a workbook, upload to Google Sheets, verify a sheet, and so on. Each one is a class
> with a Pydantic model describing its arguments, and that model *is* the JSON Schema I send
> the LLM, so the schema and the validation can never drift apart.
>
> **Second, the agent.** A planning pass produces an ordered plan constrained to a JSON
> schema — goal, steps, and a declared fallback per step. Then a tool-calling loop: the model
> picks a tool, I execute it, I feed the result back, repeat until it stops asking for tools.
> Its last message is the status report.
>
> **Third, the surfaces.** One event bus with four subscribers: the terminal UI, a
> Server-Sent Events stream, a browser page, and structured JSON logs. The agent has no idea
> which is attached.
>
> The important property is that the *registry* is the only thing that knows the toolbox. If
> I disable Excel in a YAML file, the model never sees that tool, so it plans differently and
> reports honestly that it couldn't open Excel. That's the difference between an agent and a
> script with an LLM bolted on."

## 2.2 The three things worth volunteering

**1. Excel automation is real.** It's `DispatchEx("Excel.Application")` over COM, doing an
actual *Data → From Text* import via a `QueryTable` with a UTF-8 code page — not a library
quietly writing an `.xlsx`. You can watch the window appear. On a machine without Excel it
falls back to `openpyxl` and *says* `excel_launched: false`, because a report that claims
success it can't evidence is worse than a failure.

**2. The error model is the interesting part.** `ToolError` carries a `retryable` flag.
Transient things — Excel busy with a modal dialog, an HTTP 429 — retry with exponential
backoff and jitter. Permanent things — missing file, missing credentials, Sheets API not
enabled — fail on the *first* attempt and go straight back to the model with a `remediation`
string, so it can re-plan instead of burning three identical calls. A partial success reports
as `partial` with exit code 1, not as a binary pass/fail.

**3. Verification, not assumption.** After writing to each destination the agent re-reads it
— re-opens the `.xlsx` from disk, and reads the Sheet back through the API — and compares row
counts and headers. So the final report says *verified*, and a silent truncation would be
caught rather than reported as success.

## 2.3 Likely questions, and honest answers

**"Why no LangChain / CrewAI / AutoGen?"**
> The brief allowed building my own framework and I wanted the reasoning to be inspectable.
> The whole loop is about 120 readable lines in `agent/executor.py` — you can see exactly
> where tool results are fed back, where retries happen, where the iteration cap is. With a
> framework, most of my code would be adapting to its abstractions, and debugging a bad tool
> call means reading someone else's callback stack. For an assessment about agent design,
> showing the loop seemed more useful than hiding it.

**"How does the agent actually decide what to call?"**
> Native tool calling. I send the tool schemas with each request; the model returns
> `tool_use` blocks; I execute them and return `tool_result` blocks. The decision is the
> model's. What I control is the *quality of the interface* — the tool descriptions say when
> to use each tool, not just what it does, and every result includes ground truth like
> `engine` and `excel_launched` so the model can't misreport.

**"What happens if Excel isn't installed?"**
> `excel_probe` answers that definitively before the agent commits — it's a registry check,
> so it's instant and has no side effects. If Excel is unavailable, `excel_import_csv`
> transparently falls back to `openpyxl`, still produces a valid workbook, and returns
> `excel_launched: false` with a note telling the model to report the fallback. The system
> prompt forbids claiming a step succeeded without tool evidence.

**"How do you know the Google Sheets import worked?"**
> `google_sheets_verify` reads the sheet back through the API and compares the header row and
> the row count against what was written. Same for Excel. Both are called exactly once each —
> the prompt says verify once and move on, because over-verification wastes turns.

**"Why Groq?"**
> It's free and fast, so the project runs at zero cost on a fresh clone, and its models
> support tool calling. But the executor depends on an `LLMClient` protocol, not on any
> vendor SDK — Claude, OpenAI, OpenRouter, Together and local Ollama are one environment
> variable away. That abstraction absorbs a genuine incompatibility, too: Anthropic wants all
> tool results for a turn batched into one message, OpenAI wants one message per call. The
> protocol returns a *list* of messages so each adapter does the right thing.

**"Doesn't a smaller open model handle this worse?"**
> Yes, and I handled that explicitly rather than hoping. Three things: a short
> provider-aware *Operating notes* block is appended to the system prompt for non-Claude
> providers — one tool per turn, reuse paths from tool results, verify once, then stop.
> Parallel tool calls are off by default because small models fan out into duplicates. And if
> a model emits malformed JSON in `function.arguments`, the adapter logs it and passes empty
> arguments so the tool's own validation reports the problem back to the model, rather than
> crashing the run.

**"Why is there both a `requirements.txt` and a `pyproject.toml`?"**
> Because the brief asks for a requirements file and the Dockerfile installs it as its own
> cached layer — but the list is only declared once. `pyproject.toml` reads it via
> `[tool.setuptools.dynamic]`. They were duplicated in an earlier version and had already
> drifted, which is exactly why I collapsed it.

**"Is it safe? What about prompt injection?"**
> Model output is treated as untrusted throughout. Tool arguments go through Pydantic
> validation before any tool runs. File paths are resolved against a workspace directory.
> Credentials are never in the prompt — the Google client resolves them from disk. In the
> browser UI everything is inserted with `textContent`; the one place that builds HTML
> escapes first and styles second, with a test asserting a raw `<script>` in a report can't
> survive. What I have *not* built is an approval gate for destructive actions — right now
> every tool is auto-approved, which is fine because none of them delete anything. If tools
> could delete or send, I'd gate those behind confirmation.

**"How would you take this to production?"**
> Three changes. Run state in the HTTP API is in-process, so it'd move to Redis or Postgres.
> Sheets uploads are a single `values.update` call — fine for thousands of rows, but a large
> dataset should be chunked. And Excel COM needs a Windows worker, so I'd split it: a Linux
> container for the API and Google Sheets, and a Windows worker pulling Excel jobs off a
> queue. The tool registry already makes that split clean, since tools don't know what's
> calling them.

**"What are you not happy with?"**
> Two things. I had no Groq key on the build machine, so that path is verified by 33 unit
> tests plus an end-to-end run against a stub HTTP server that asserts the exact request JSON
> — but not against the live API. Excel and Google Sheets *were* exercised for real. And
> `excel_tools.py` sits at 51% coverage without `-m slow`, because the COM path needs real
> Excel; with the slow tests it's 85%.

## 2.4 If something breaks live

| Problem | Say this, do that |
|---|---|
| Groq rate-limits you (429) | "Free tier has per-minute caps — the agent retries with backoff." Re-run with `--no-plan` to spend one fewer call. |
| A model name 404s | Groq rotates models. `agentic-sheets run "..." --model openai/gpt-oss-120b`. |
| Excel throws a modal dialog | Close it — the retry policy is designed for exactly this, and you'll see `↻` in the trace. |
| Google 403 | "That's the API-not-enabled case." Show the remediation text the agent printed. |
| The run is slow | `--no-plan` skips the planning call. Say so rather than sitting in silence. |
| Everything fails | `agentic-sheets doctor` — it isolates which prerequisite broke. Have a completed run in a second terminal as a backup. |

## 2.5 Where to point in the code

| If they ask about | Open |
|---|---|
| The agent loop | `src/agentic_sheets/agent/executor.py` |
| Planning | `src/agentic_sheets/agent/planner.py` + `prompts.py` (the plan JSON schema) |
| A tool | `src/agentic_sheets/tools/excel_tools.py` (COM + fallback + verify) |
| Provider independence | `src/agentic_sheets/llm/base.py` (the protocol), then the two adapters |
| Error handling | `src/agentic_sheets/errors.py` + `retry.py` |
| Configurable tools | `config/tools.yaml` + `tools/registry.py` |
| Tests worth showing | `tests/test_executor.py` (the loop) and `tests/test_groq_wire_format.py` (exact JSON on the wire) |
