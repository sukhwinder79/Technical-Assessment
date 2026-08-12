# Agentic Spreadsheet Agent

An autonomous AI agent that turns **one natural-language instruction** into a completed
multi-application workflow: generate employee data, drive the real **Microsoft Excel**
application through COM automation, and push the same data to **Google Sheets** via the
Sheets API — then verify both destinations and report per-step success or failure.

```console
$ agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets."
```

The model decides which tools to call and in what order. There is no hard-coded pipeline:
disable a tool in `config/tools.yaml`, or run on a host without Excel, and the agent
re-plans around it and reports honestly what it could not do.

**Runs for free.** The default provider is **Groq**, whose API key is free with no credit
card — so a fresh clone works at zero cost. Claude, OpenAI, OpenRouter, Together and local
Ollama are one environment variable away ([switching providers](#switching-llm-provider)).

---

## Contents

- [What it actually does](#what-it-actually-does)
- [Architecture](#architecture)
- [Quickstart](#quickstart)
- [Switching LLM provider](#switching-llm-provider)
- [Google Sheets setup](#google-sheets-setup)
- [Usage](#usage)
- [Example prompts](#example-prompts)
- [How the agent works](#how-the-agent-works)
- [Configuration reference](#configuration-reference)
- [Web UI](#web-ui)
- [HTTP API](#http-api)
- [MCP server](#mcp-server)
- [Docker](#docker)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)
- [Assessment requirements checklist](#assessment-requirements-checklist)
- [Known limitations](#known-limitations)

---

## What it actually does

Given the prompt above, a run looks like this (real output, trimmed):

```
┌───────────────────────── Agentic Spreadsheet Agent ─────────────────────────┐
│ Instruction  Create a sample employee CSV and import it into Excel and …    │
│ Model        claude-opus-5                                                  │
│ Run / session  76a682f042df / session-3b91c4de                              │
└─────────────────────────────────────────────────────────────────────────────┘
· Planning the work before touching any tool…
┌──────────────────────────────── Plan ───────────────────────────────────────┐
│ Generate 25 sample employee records, load them into Excel and Google Sheets │
│ #  Step                    Tool                   Detail                    │
│ 1  Generate employee CSV   generate_employee_csv   25 rows, 5+ columns       │
│ 2  Import into Excel       excel_import_csv        Launch Excel, save .xlsx  │
│ 3  Verify the workbook     excel_verify_workbook   Re-open, confirm 25 rows  │
│ 4  Upload to Google Sheets google_sheets_import    Create sheet, write rows  │
│ 5  Verify the sheet        google_sheets_verify    Read back, confirm rows    │
└─────────────────────────────────────────────────────────────────────────────┘
· Thinking (turn 1/25)…
  → generate_employee_csv(row_count=25, filename=employees.csv)
  ✔ generate_employee_csv → 25 rows → employees.csv (0.0s)
· Thinking (turn 2/25)…
  → excel_import_csv(csv_path=…\employees.csv, workbook_filename=employees.xlsx)
  → Launching Microsoft Excel and importing employees.csv…
  ✔ excel_import_csv → 25 rows · employees.xlsx · engine=excel-com (4.2s)
  → excel_verify_workbook(workbook_path=…\employees.xlsx, expected_row_count=25)
  ✔ excel_verify_workbook → 25 rows · employees.xlsx · verified (0.3s)
· Thinking (turn 3/25)…
  → google_sheets_import(csv_path=…\employees.csv)
  ✔ google_sheets_import → 25 rows — https://docs.google.com/spreadsheets/d/1AbC…

                                 Step report
 #   Tool                    Status    Time  Detail
 1   generate_employee_csv   SUCCESS   0.0s  25 rows → employees.csv
 2   excel_import_csv        SUCCESS   4.2s  25 rows · employees.xlsx · engine=excel-com
 3   excel_verify_workbook   SUCCESS   0.3s  25 rows · employees.xlsx · verified
 4   google_sheets_import    SUCCESS   2.1s  25 rows — https://docs.google.com/…
 5   google_sheets_verify    SUCCESS   0.9s  verified 25 rows — https://docs.google.com/…

COMPLETED  5/5 steps ok · 4 model turns · 12.6s · 18420 in / 2210 out tokens
```

**Excel automation is real.** `excel_import_csv` starts `Excel.Application` over COM,
performs an actual *Data → From Text* import (a `QueryTable` with UTF-8 code page and
comma delimiter), formats the sheet (bold header, freeze panes, AutoFilter, currency and
date number formats), and saves an `.xlsx`. It is not a library writing a file behind
the scenes — you can watch the Excel window appear.

---

## Architecture

```
                      ┌──────────────────────────────────────────┐
   natural language   │  Planner  (structured output → JSON)     │
   instruction  ──────▶  "goal, ordered steps, per-step fallback"│
                      └────────────────────┬─────────────────────┘
                                           │ plan injected into system prompt
                      ┌────────────────────▼─────────────────────┐
                      │  Executor — native tool-calling loop     │
                      │                                          │
                      │  model turn ──▶ tool_use blocks          │
                      │       ▲                │                 │
                      │       │                ▼                 │
                      │  tool_result ◀── registry + retry/backoff │
                      └────────┬─────────────────────────────────┘
                               │
        ┌──────────────────────┼───────────────────────┐
        ▼                      ▼                       ▼
   Tool registry          Event bus              Session memory
   (config/tools.yaml)    │                      (JSON on disk)
        │                 ├── Rich console         · conversation
        │                 ├── SSE (FastAPI)        · working facts
        │                 └── structured logs      · run history
        ▼
  ┌───────────┬──────────────┬───────────────┬──────────────────┐
  │ data      │ Excel (COM)  │ Google Sheets │ verification     │
  │ CSV/XLSX/ │ launch,      │ Sheets + Drive│ re-read both     │
  │ ODS       │ import, save │ API           │ destinations     │
  └───────────┴──────────────┴───────────────┴──────────────────┘
```

Design choices worth calling out:

| Concern | Approach |
|---|---|
| Agent framework | Hand-written loop over **native tool calling**. No LangChain/CrewAI — the loop is ~120 readable lines and every decision point is inspectable. |
| Provider coupling | The executor depends on an `LLMClient` protocol ([`llm/base.py`](src/agentic_sheets/llm/base.py)), never on a vendor SDK. Two adapters ship: Claude (adaptive thinking, effort) and OpenAI-compatible (Groq, OpenAI, OpenRouter, Together, Ollama). The protocol even absorbs the fact that Anthropic wants tool results *batched* into one message while OpenAI wants *one message per call*. |
| Tool contract | Each tool is a class with a Pydantic args model that **doubles as the JSON Schema** sent to the API — schema and validation cannot drift apart. |
| Failure handling | `ToolError(retryable=…)` distinguishes transient (COM busy, HTTP 429/5xx) from permanent (missing file, bad credentials). Transient failures retry with exponential backoff + jitter; permanent ones go straight back to the model with a `remediation` hint so it can *re-plan* instead of retrying blindly. |
| Honesty | Tools return the ground truth (`excel_launched: false`, `engine: openpyxl`) and the system prompt forbids claiming a step succeeded without tool evidence. |

---

## Quickstart

### Prerequisites

| | |
|---|---|
| Python | 3.10+ (developed and tested on 3.12) |
| An LLM API key | **Free:** <https://console.groq.com/keys> — no credit card, takes about 30 seconds. Or use Claude/OpenAI/OpenRouter/Together/Ollama instead. |
| Microsoft Excel | Required only for real Excel automation. Windows + desktop Excel. Without it the agent falls back to `openpyxl` and says so. |
| Google Cloud project | Required only for the Google Sheets step. See [below](#google-sheets-setup). |

### Install

```powershell
git clone <your-repo-url> Technical-Assessment
cd Technical-Assessment

python -m venv .venv
.\.venv\Scripts\Activate.ps1        # macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
pip install -e .                    # optional: adds the `agentic-sheets` command
```

`pip install -e .` is a convenience, not a requirement. [`main.py`](main.py) puts `src/` on
the path itself, so everything works from a bare clone:

| With the install | Without it |
|---|---|
| `agentic-sheets run "..."` | `python main.py run "..."` |
| `agentic-sheets doctor` | `python main.py doctor` |
| `agentic-sheets serve --open` | `python main.py serve --open` |

**`python main.py` with no arguments opens the web UI** — it deliberately does *not* start an
agent run. A run costs real API tokens, so it should never be what happens by accident when
someone types the obvious command to look at the project. The UI costs nothing until you
press Run.

### Configure

```powershell
copy .env.example .env              # macOS/Linux: cp .env.example .env
```

Edit `.env` and set at minimum:

```ini
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...
```

Grab that key from <https://console.groq.com/keys> — free, no card. (Prefer Claude?
`LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY=sk-ant-...`.)

### Verify the environment

```powershell
agentic-sheets doctor
```

```
                             Environment check
┌─────────────────┬────────┬─────────────────────────────────────────────────┐
│ LLM provider    │ OK     │ groq  (free tier) · https://api.groq.com/openai… │
│ API key         │ OK     │ GROQ_API_KEY set (gsk_abcd…7f2c)                │
│ Model           │ OK     │ llama-3.3-70b-versatile                         │
│ Workspace       │ OK     │ D:\Technical_Assessment\workspace               │
│ Microsoft Excel │ OK     │ Microsoft Excel is registered for COM (16.0)    │
│ Google Sheets   │ OK     │ credentials load OK (mode=service_account)      │
│ Tools           │ OK     │ 8 enabled of 8                                  │
└─────────────────┴────────┴─────────────────────────────────────────────────┘

Ready to run.
```

`doctor` is the first thing to run when something is off — it checks each prerequisite
independently and tells you exactly what to fix.

### Run it

```powershell
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets."
```

Generated files land in `workspace/`. Exit code is `0` (all steps succeeded),
`1` (partial) or `2` (failed) so the command composes in scripts and CI.

---

## Switching LLM provider

The agent talks to an `LLMClient` protocol, so the provider is configuration, not code.
Two adapters ship — Claude, and one that covers every OpenAI-compatible API.

| `LLM_PROVIDER` | Key variable | Default model | Cost |
|---|---|---|---|
| `groq` *(default)* | `GROQ_API_KEY` | `llama-3.3-70b-versatile` | **Free tier** |
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-opus-5` | Paid |
| `openai` | `OPENAI_API_KEY` | `gpt-4o-mini` | Paid |
| `openrouter` | `OPENROUTER_API_KEY` | `meta-llama/llama-3.3-70b-instruct` | Free models available |
| `together` | `TOGETHER_API_KEY` | `meta-llama/Llama-3.3-70B-Instruct-Turbo` | Paid |
| `ollama` | *(none)* | `llama3.1:8b` | Free, fully local |
| `custom` | `LLM_API_KEY` + `LLM_BASE_URL` | — | — |
| `auto` | first key found | provider default | prefers free Groq |

Per run, without touching `.env`:

```powershell
agentic-sheets run "..." --provider groq
agentic-sheets run "..." --provider anthropic --model claude-opus-5 --effort medium
agentic-sheets run "..." --provider ollama --model qwen2.5:14b     # offline
```

### Choosing a Groq model

Any Groq model with tool-calling support works. Current list:
<https://console.groq.com/docs/models>.

| Model | Notes |
|---|---|
| `llama-3.3-70b-versatile` | Default. Best all-round tool calling on the free tier. |
| `openai/gpt-oss-120b` | Stronger multi-step reasoning; slower. |
| `llama-3.1-8b-instant` | Fastest, but noticeably weaker at long tool loops. |

```powershell
agentic-sheets run "..." --model openai/gpt-oss-120b
```

If a model has been decommissioned you get a clear error naming `LLM_MODEL` and the
provider's model list rather than a raw 404.

### What differs on a smaller open model

Nothing about the workflow — the whole assessment completes on Groq's free tier. But four
accommodations were needed, and each came out of an actual failure on a live run rather than
from guesswork:

- **JSON Schemas are flattened.** Pydantic renders an optional `int` as
  `{"anyOf": [{"type": "integer"}, {"type": "null"}], "default": null}`. Llama mis-generated
  against that union — it emitted `<function=generate_employee_csv [{"seed": null, …}]`, an
  *array* where an object belongs, and Groq rejected the whole turn with
  `400 tool_use_failed`. `simplify_schema()` collapses nullable unions to their one real type
  and drops `title`/`default` noise; optionality is preserved through `required`, which is the
  part that matters. Zero unions survive in any of the eight tool schemas.
- **Unparsable tool calls are re-sampled, not fatal.** `tool_use_failed` is sampling variance,
  so the turn is retried up to three times before failing with advice to try a stronger model.
- **Parallel tool calls off by default** (`LLM_DISABLE_PARALLEL_TOOL_CALLS=true`). Small models
  fan out into duplicate calls; one per turn is markedly more reliable.
- **Provider-aware prompting.** A short *Operating notes* block is appended for non-Anthropic
  providers (one tool per turn, reuse paths from tool results, verify once, then stop), phrased
  as positive guidance. Frontier models don't get it — they don't need it.

Malformed `function.arguments` JSON is also survivable: the adapter logs it and passes empty
arguments so the tool's own Pydantic validation reports the problem back to the model.

`AGENT_EFFORT` and adaptive thinking are Claude-only and are never sent to other providers —
sending them would be a 400.

### Free-tier token budget

Groq's free tier caps **tokens per minute**, not just requests, and an agent loop resends the
whole conversation every turn. Two things keep runs inside it:

- **Tool results are trimmed before they re-enter context.** Row previews, sample rows and
  per-check expected/actual arrays are dropped — the model needs to know a step worked and
  what the paths are, not to re-read data it just wrote. The console and the JSON report still
  show everything.
- **The system prompt lists tool *names* only.** Full descriptions already travel with the
  schemas; repeating them would pay for the same tokens twice every turn.

If you still hit the cap, the agent tells you what to change. Fastest fixes: wait a minute and
resume with `--session <name> --continue` (earlier steps are remembered, so nothing is redone),
or add `--no-plan`, which removes one whole request from the run.

---

## Google Sheets setup

> ### Read this first: service accounts cannot create spreadsheets
>
> Google removed free Drive storage from service accounts. A service account's
> `storageQuota.limit` is `0`, and since a spreadsheet *is* a Drive file that someone must
> own, `spreadsheets.create` fails with a bare `403 The caller does not have permission` —
> even with both APIs correctly enabled. It is a platform constraint, not a misconfiguration,
> and the bare 403 sends most people hunting the wrong problem. The agent detects this exact
> case (it checks the quota on failure) and names both fixes in its error.
>
> So: **pick Option A if you want the agent to create sheets.** Pick Option B if you want a
> headless setup and are happy for it to write into a sheet you already own.

### Option A — OAuth desktop flow (recommended)

Sheets are created in **your own** Drive, using your own storage. Works with a plain Gmail
account. One browser consent, then a cached token — no prompts after that.

1. [Google Cloud console](https://console.cloud.google.com) → create or select a project.
2. Enable both APIs:
   - <https://console.cloud.google.com/apis/library/sheets.googleapis.com>
   - <https://console.cloud.google.com/apis/library/drive.googleapis.com>
3. **APIs & Services → OAuth consent screen** → External → fill in the app name and your
   email → Save. Under **Audience → Test users**, add your own Google address.
4. **Credentials → Create credentials → OAuth client ID → Application type: Desktop app** →
   Create → **Download JSON**.
5. Save it as `credentials/oauth_client.json`.
6. In `.env`:

   ```ini
   GOOGLE_AUTH_MODE=oauth
   GOOGLE_OAUTH_CLIENT_FILE=./credentials/oauth_client.json
   GOOGLE_TOKEN_FILE=./credentials/token.json
   ```

The first run opens a browser once; `token.json` is written and reused afterwards.

### Option B — service account, writing into a sheet you own

Headless and repeatable — no browser, good for servers and CI. The account cannot *create*
a spreadsheet (see above), so you point it at one you own.

1. [Google Cloud console](https://console.cloud.google.com) → create or select a project.
2. Enable both APIs (links in Option A).
3. **Credentials → Create credentials → Service account.** Any name; no roles needed.
4. Open it → **Keys → Add key → Create new key → JSON** → download.
5. Save it as `credentials/service_account.json`. Note the `client_email` inside it, which
   looks like `something@your-project.iam.gserviceaccount.com`.
6. Create a blank Google Sheet in your own Drive. **Share** it with that `client_email`
   address as **Editor**. Copy its id from the URL:
   `docs.google.com/spreadsheets/d/`**`<THIS PART>`**`/edit`
7. In `.env`:

   ```ini
   GOOGLE_AUTH_MODE=service_account
   GOOGLE_CREDENTIALS_FILE=./credentials/service_account.json
   GOOGLE_SPREADSHEET_ID=<the id from step 6>
   ```

`GOOGLE_SHARE_WITH_EMAIL` is only used for sheets the agent creates itself, so it is not
needed in this mode.

> A Google Workspace **Shared Drive** removes the limitation — the Drive owns the storage,
> not the account — so a service account can create sheets inside one. That is the right
> setup for a real deployment.

### Option C — skip Google Sheets

```ini
GOOGLE_AUTH_MODE=disabled
```

The agent completes the Excel half and reports the Google Sheets step as skipped with the
reason. Useful for demonstrating graceful degradation.

---

## Usage

### CLI

```powershell
# The assessment prompt (also the default if you pass no argument)
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets."

# Named session — the agent remembers earlier runs and their artifacts
agentic-sheets run "Generate 40 employee records and load them into Excel" --session demo
agentic-sheets run "Now export that same data to ODS as well" --session demo --continue

# Cheaper / faster, and skip the planning pass
agentic-sheets run "..." --effort low --no-plan

# Machine-readable report for CI
agentic-sheets run "..." --json-out reports/run.json

# Interactive multi-turn mode
agentic-sheets chat --session demo

# Inspect and diagnose
agentic-sheets tools              # the live toolbox
agentic-sheets tools --schemas    # full JSON Schema per tool
agentic-sheets doctor             # environment check
agentic-sheets sessions           # stored sessions
agentic-sheets sessions --show demo

# Other surfaces
agentic-sheets serve --open        # web UI + REST API on http://127.0.0.1:8000
agentic-sheets mcp                 # MCP server over stdio
```

Every command also works as `python -m agentic_sheets ...` if you skipped `pip install -e .`.

### `run` options

| Flag | Purpose |
|---|---|
| `--session, -s` | Reuse a named session (memory across runs). |
| `--continue, -c` | Continue that session's conversation rather than starting a fresh turn. |
| `--provider, -p` | Override `LLM_PROVIDER` for one run. |
| `--model, -m` | Override `LLM_MODEL` for one run. |
| `--effort, -e` | `low` \| `medium` \| `high` \| `xhigh` \| `max`. Anthropic only. |
| `--no-plan` | Skip the planning pass (single-phase execution). |
| `--quiet, -q` | Hide the model's streamed narration. |
| `--json-out` | Write the full structured run report to a file. |

---

## Example prompts

All of these work against the same toolbox — the agent picks a different path for each.
More, with expected outcomes, in [`examples/prompts.md`](examples/prompts.md).

```text
Create a sample employee CSV and import it into Excel and Google Sheets.

Create an employee CSV and import it into Excel and Google Sheets.

Generate 50 employee records with realistic salaries, open them in Excel, save the
workbook, then upload the same data to a Google Sheet called "Q3 Headcount" and
confirm both imports.

I need sample HR data in three formats — CSV, XLSX and ODS — plus a Google Sheet.
Verify every destination and tell me the row counts.

Build a 25-row employee dataset, import it into Excel only (skip Google Sheets),
and confirm the workbook has the right headers.

Use reproducible data (seed 42) so I get the same 30 rows every time, then load it
into Excel and Google Sheets.
```

---

## How the agent works

### 1. Multi-step planning before execution

Before any tool runs, a separate LLM call produces a plan constrained to a JSON Schema
(`goal`, ordered `steps` with a `tool` and a **declared fallback** each, plus `risks`).
The plan is printed for the user and injected into the executor's system prompt.

Planning is **advisory**: if it fails the run continues without it, and if a tool result
contradicts a planned assumption the executor is told to adapt rather than follow the plan
off a cliff. Disable with `--no-plan` or `AGENT_PLANNING=false`.

### 2. Tool-calling loop

[`agent/executor.py`](src/agentic_sheets/agent/executor.py):

```
model turn  →  stop_reason?
                ├─ tool_use    → execute every tool_use block (in parallel where the
                │                model asked for it), return ALL results in ONE user
                │                message, loop
                ├─ pause_turn  → re-send to let a server-side tool continue
                ├─ max_tokens  → flag truncation in the report
                └─ end_turn    → the final text is the status report
```

Details that matter:

- Assistant content is echoed back **verbatim** so adaptive-thinking blocks keep their
  signatures.
- All tool results for one turn go back in a **single** user message — splitting them
  teaches the model to stop issuing parallel calls.
- A hallucinated tool name is returned as an error with the valid names, not a crash.
- Oversized tool results shed their bulky preview fields before truncation, so what the
  model receives is always valid JSON.
- `AGENT_MAX_ITERATIONS` (default 25) bounds the loop.

### 3. Retry logic

`RetryPolicy` — exponential backoff with jitter, capped. **Only retryable failures
retry:**

| Failure | Retryable | Why |
|---|---|---|
| Excel COM `RPC_E_CALL_REJECTED` (busy/modal dialog) | ✅ | Usually clears in a second |
| Google API 429 / 500 / 503 | ✅ | Transient |
| Workbook still being flushed to disk | ✅ | Race with Excel's writer |
| CSV not found, bad arguments | ❌ | Retrying is pointless |
| Missing/invalid Google credentials | ❌ | Needs a human |
| Sheets/Drive API not enabled | ❌ | Needs a console change |

Per-tool budgets are configurable in `config/tools.yaml` (network tools get more).

### 4. Memory

Per-session JSON under `.agent_memory/`, two layers:

- **Conversation** — the verbatim message list, so `--continue` resumes with full context.
- **Working facts** — `last_csv_path`, `last_workbook_path`, `last_spreadsheet_url`, …
  written by tools and injected into the system prompt. This is what makes
  *"now also export that to ODS"* resolve without re-deriving anything.

Plus a run history (instruction + status + artifacts) so the agent knows what it did before.

### 5. Configurable tools

`config/tools.yaml` enables/disables tools and overrides retries and descriptions —
**no code changes**. Disabled tools are removed from the schema list the model ever sees,
so this changes what the agent can *plan*, not just what it can call:

```yaml
tools:
  excel_import_csv:
    enabled: false      # the agent now reports Excel as unavailable and re-plans
```

### 6. Progress updates

One event bus, three subscribers: the Rich console, the FastAPI SSE stream, and structured
logs. Nothing in the agent core knows which front-end is attached.

### 7. Structured logging

JSON lines to `logs/agent-YYYY-MM-DD.jsonl` (always) plus pretty console output
(`LOG_JSON=true` for JSON on stderr too). Every line carries the `run_id`:

```bash
jq 'select(.run_id=="76a682f042df")' logs/agent-2026-08-11.jsonl
```

### The toolbox

| Tool | What it does |
|---|---|
| `generate_employee_csv` | 1–5000 realistic employee rows (default 25). Always includes Employee ID, Name, Department, Email, Salary; optionally Job Title, Location, Hire Date, Employment Type, Performance Rating. Salaries are department-appropriate; emails are unique and ASCII-safe; `seed` makes it reproducible. |
| `convert_spreadsheet` | CSV ⇄ XLSX ⇄ ODS without Excel. Writes real numbers/dates, styled header, freeze panes, AutoFilter. |
| `read_csv_preview` | Header, row count and first N rows of any CSV. |
| `excel_probe` | Is Excel automatable here? Instant registry check by default; `deep_check=true` launches it. |
| `excel_import_csv` | **Launches Excel**, imports via `QueryTable`, formats, saves `.xlsx`. Falls back to `openpyxl` with `excel_launched: false` when Excel is unavailable. |
| `excel_verify_workbook` | Re-opens the saved workbook and checks sheet names, headers, row count. |
| `google_sheets_import` | Creates/updates a spreadsheet via the Sheets API, formats the header, shares it via the Drive API. |
| `google_sheets_verify` | Reads the sheet back and checks headers and row count. |

---

## Configuration reference

All settings are environment variables (or `.env`). See [`.env.example`](.env.example).

| Variable | Default | Notes |
|---|---|---|
| `LLM_PROVIDER` | `groq` | `groq` \| `anthropic` \| `openai` \| `openrouter` \| `together` \| `ollama` \| `custom` \| `auto`. |
| `GROQ_API_KEY` | — | **Required for the default provider.** Free at <https://console.groq.com/keys>. |
| `LLM_MODEL` | *(provider default)* | Blank uses the provider's default. |
| `LLM_BASE_URL` | *(provider default)* | Override for proxies or `custom`. |
| `LLM_API_KEY` | — | Generic key override; wins over provider-specific keys. |
| `LLM_DISABLE_PARALLEL_TOOL_CALLS` | `true` | Keeps small models to one tool call per turn. |
| `ANTHROPIC_API_KEY` | — | Required when `LLM_PROVIDER=anthropic`. |
| `AGENT_MODEL` | — | Legacy alias for `LLM_MODEL`; still honoured. |
| `AGENT_EFFORT` | *(API default)* | `low`…`max`. **Anthropic only** — ignored elsewhere. |
| `AGENT_MAX_TOKENS` | `8000` | Per model turn. |
| `AGENT_MAX_ITERATIONS` | `25` | Loop bound. |
| `AGENT_PLANNING` | `true` | Planning pass on/off. |
| `WORKSPACE_DIR` | `./workspace` | Where generated files go. |
| `LOG_DIR` / `MEMORY_DIR` | `./logs` / `./.agent_memory` | |
| `TOOLS_CONFIG` | `./config/tools.yaml` | |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `false` | |
| `EXCEL_VISIBLE` | `true` | Show the Excel window (good for the demo). |
| `EXCEL_KEEP_OPEN` | `true` | Leave Excel open after saving. |
| `GOOGLE_AUTH_MODE` | `service_account` | `service_account` \| `oauth` \| `disabled`. |
| `GOOGLE_CREDENTIALS_FILE` | `./credentials/service_account.json` | |
| `GOOGLE_SHARE_WITH_EMAIL` | — | Strongly recommended with a service account. |
| `GOOGLE_SPREADSHEET_ID` | — | Always write into this sheet instead of creating one. |
| `TOOL_MAX_RETRIES` | `2` | Global default; per-tool overrides in `tools.yaml`. |
| `TOOL_RETRY_BASE_DELAY` | `1.0` | Seconds. |

---

## Web UI

```powershell
agentic-sheets serve --open        # http://127.0.0.1:8000
```

A single self-contained page — no CDN, no framework, no build step — so it works offline
and inside the Docker image. It is the **fourth subscriber to the same event bus** the CLI,
the SSE stream and the structured logs already read, so it shows the run unfolding rather
than polling for a result.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Agentic Spreadsheet Agent            ● provider groq  ● excel COM ready  │
│ One instruction → CSV, Excel, Sheets  ● model llama-3.3-70b  ● tools 8   │
├──────────────────────────────────────────────────────────────────────────┤
│ INSTRUCTION                                                              │
│ ┌──────────────────────────────────────────────────────────────────────┐ │
│ │ Create a sample employee CSV and import it into Excel and Google …   │ │
│ └──────────────────────────────────────────────────────────────────────┘ │
│ ⟨ example prompts as clickable chips ⟩                                   │
│ Provider ▾  Model ▾  Session   ☑ Plan first  ☐ Continue   [ Run agent ]  │
├──────────────────────────────────────────────────────────────────────────┤
│ PLAN                                              5 steps                │
│ 1. Generate employee CSV  generate_employee_csv                          │
│ 2. Import into Excel      excel_import_csv                               │
│    fallback: convert_spreadsheet with target_format=xlsx                 │
├──────────────────────────────────────────────────────────────────────────┤
│ ACTIVITY                                          run 5dc62ba9f3ef       │
│  → generate_employee_csv(row_count=25, filename=employees.csv)           │
│  ✔ generate_employee_csv → 25 rows → employees.csv          0.0s         │
│  → Launching Microsoft Excel and importing employees.csv…                │
│  ✔ excel_import_csv → 25 rows · employees.xlsx · engine=excel-com  4.2s  │
├──────────────────────────────────────────────────────────────────────────┤
│ RESULT                                   COMPLETED  5/5 steps ok · 12.6s │
│ #  Tool                   Status   Time   Detail                         │
│ 1  generate_employee_csv  SUCCESS  0.0s   25 rows → employees.csv        │
│ …                                                                        │
│ ARTIFACTS   last_spreadsheet_url  https://docs.google.com/… ← clickable  │
└──────────────────────────────────────────────────────────────────────────┘
```

What it gives you:

- **A workspace file browser that costs nothing.** Every CSV/XLSX/ODS the agent has written,
  with **View data** (reads the file back from disk and renders it as a table in the browser —
  so you can see what Excel actually saved) and **Download**. Available on page load, before
  any run, which makes it the cheap way to inspect earlier output.
- **A model picker with the trade-offs written on it** — per-minute budgets, and an explicit
  warning on the one model that cannot tool-call, so a bad choice is caught before it wastes
  a run rather than after.
- **A live elapsed timer** on the Run button, because a three-minute rate-limited run
  otherwise reads as a hang.
- **The plan up front**, including each step's declared fallback.
- **A live trace** — tool calls, retries in amber, failures in red — streamed over SSE while
  the agent works, identical to what the CLI renders.
- **The model's narration** as it decides what to do next.
- **A per-step report** with statuses, timings and retry counts, then the artifacts, with the
  Google Sheets URL as a real link you can click on camera.
- **Provider switching from the browser** — run the same prompt on Groq, then Claude, without
  touching `.env`.

The page never trusts model output: everything is inserted via `textContent`, and the one
place that builds HTML (the mini-markdown renderer for the final report) escapes first and
styles second. A test asserts that raw `<script>` in a report cannot survive.

> The brief did not ask for a UI. It exists because the SSE stream was already there, and a
> browser makes the demo video considerably clearer than a terminal.

---

## HTTP API

```powershell
agentic-sheets serve          # http://127.0.0.1:8000  ·  /docs for OpenAPI
```

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The [web UI](#web-ui). |
| `GET` | `/health` | Liveness + environment summary. |
| `GET` | `/tools` | Live toolbox with JSON Schemas. |
| `POST` | `/runs` | Start a run, return `202` immediately. |
| `GET` | `/runs/{id}` | Status and full structured report. |
| `GET` | `/runs/{id}/events` | **Server-Sent Events** — the same progress stream the CLI renders. |
| `POST` | `/runs/sync` | Run and block. `200` completed · `207` partial · `500` failed. |
| `GET` | `/runs` | Runs in this process. |

```bash
# Fire and follow
RUN=$(curl -s -X POST localhost:8000/runs \
  -H 'content-type: application/json' \
  -d '{"instruction":"Create an employee CSV and import it into Excel and Google Sheets."}' \
  | jq -r .run_id)

curl -N localhost:8000/runs/$RUN/events     # live progress
curl -s localhost:8000/runs/$RUN | jq .result.steps
```

A late SSE subscriber gets the whole event history replayed, so you never miss the start
of a run.

---

## MCP server

The same tool registry is published over the **Model Context Protocol**, so an MCP host
(Claude Desktop, Claude Code, …) can drive Excel and Google Sheets with its own model
doing the orchestration.

```powershell
agentic-sheets mcp        # stdio transport
```

Claude Desktop — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "agentic-sheets": {
      "command": "D:\\Technical_Assessment\\.venv\\Scripts\\agentic-sheets.exe",
      "args": ["mcp"],
      "env": {
        "WORKSPACE_DIR": "D:\\Technical_Assessment\\workspace",
        "GOOGLE_AUTH_MODE": "service_account",
        "GOOGLE_CREDENTIALS_FILE": "D:\\Technical_Assessment\\credentials\\service_account.json"
      }
    }
  }
}
```

No `ANTHROPIC_API_KEY` is needed for the MCP server — the host supplies the model.
Targets the MCP 2.x server API. Covered by a real protocol handshake in
[`tests/test_mcp_server.py`](tests/test_mcp_server.py).

---

## Docker

```powershell
docker compose up --build              # UI + API on http://localhost:8000
docker compose run --rm agent-cli doctor
docker compose run --rm agent-cli run "Create an employee CSV and upload it to Google Sheets."
```

`workspace/`, `logs/` and `.agent_memory/` are bind-mounted so generated files appear on
the host; `credentials/` is mounted read-only.

> **Excel cannot run in a Linux container.** Inside the image `excel_import_csv`
> automatically falls back to `openpyxl` and reports `excel_launched: false`, which the
> agent states plainly in its report. Real Excel COM automation requires running natively
> on Windows. The container is the right home for the API and the Google Sheets half.

---

## Tests

```powershell
pytest                       # 229 tests — no API key, Excel or Google credentials needed
pytest -m slow               # + tests that launch the real Excel application
pytest --cov=agentic_sheets  # coverage (88% with -m slow included)
```

The suite runs fully offline. Both LLM providers are stubbed, so what is under test is the
**agent's control flow and our translation layers**, not the model.

| File | Covers |
|---|---|
| `test_executor.py` | The loop: dispatch, parallel calls, retries, tool failures surfaced to the model, hallucinated tool names, `pause_turn`, truncation, iteration cap, memory, artifacts, events, result serialisation. |
| `test_data_tools.py` | Row counts and the 20-row minimum, reproducible seeds, unique ASCII emails, CSV/XLSX/ODS round-trips, typed numbers, workspace confinement. |
| `test_excel_tools.py` | The `openpyxl` fallback path (everywhere), verification catching row-count and header mismatches, plus real COM automation under `-m slow`. |
| `test_sheets_tools.py` | Credential resolution and translating raw Google `HttpError`s into actionable messages. |
| `test_sheets_import.py` | Create-vs-reuse, adding a worksheet, the `values.update` payload, header formatting, Drive sharing, verification — with the Google client faked. |
| `test_anthropic_client.py` | The Claude adapter: adaptive thinking, prompt caching, response parsing, refusals, structured-output fallback, verbatim thinking-block replay. |
| `test_openai_compatible_client.py` | The Groq/OpenAI adapter: provider resolution, tool-schema conversion, malformed-JSON tolerance, error translation, message shapes. |
| `test_groq_wire_format.py` | **End-to-end against a stub HTTP server** — asserts the exact JSON sent to Groq: tool envelopes, `assistant.tool_calls`, `role: "tool"` results. |
| `test_registry.py` | Discovery, YAML enable/disable, retry overrides, schema generation, dispatch validation. |
| `test_retry.py` | What retries, what fails fast, backoff shape, callbacks. |
| `test_memory.py` | Persistence, isolation between sessions, trimming, corrupt-file tolerance. |
| `test_planner.py` | Schema validity, parsing, repairing partial steps, graceful degradation. |
| `test_console_and_events.py` | Every event type through the renderer, every run outcome, markup-injection safety. |
| `test_cli.py` | `doctor` / `tools` / `sessions` / `run`, provider overrides, exit-code contract. |
| `test_api.py` | Health, tools, sync/async runs, the SSE stream, status→HTTP mapping, and that the UI handles every event type the agent can emit. |
| `test_api_live_stream.py` | **Real agent + real tools + stub LLM over HTTP** — the exact contract the browser depends on: event names, ordering, `done`, and the report shape. |
| `test_mcp_server.py` | A real MCP handshake against the server as a subprocess. |

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No API key found for LLM_PROVIDER=groq` | Copy `.env.example` to `.env` and add `GROQ_API_KEY` (free at <https://console.groq.com/keys>). Confirm with `agentic-sheets doctor`. |
| `Model '…' was not found on groq` | That model was decommissioned or renamed. Pick one from <https://console.groq.com/docs/models> and set `LLM_MODEL`. |
| `rejected the request for exceeding its token budget` (413 / 429) | Groq's free tier caps tokens **per minute** (12,000 on `llama-3.3-70b-versatile`); a six-turn run costs ~20k in total, so back-to-back runs get rejected. Wait a minute and resume with `--session x --continue`, add `--no-plan`, or use `--model openai/gpt-oss-120b`. See [Free-tier token budget](#free-tier-token-budget). |
| `produced a tool call groq could not parse` | The model failed to emit a valid tool call three times. Use a stronger model (`--model openai/gpt-oss-120b`) or `--provider anthropic`. |
| The model loops or calls tools with odd arguments | You are probably on a small model. Try `--model openai/gpt-oss-120b`, keep `LLM_DISABLE_PARALLEL_TOOL_CALLS=true`, or switch to `--provider anthropic`. |
| Excel step reports `excel_launched: false` | Not on Windows, `pywin32` missing, or Excel not installed. `pip install pywin32`; check `agentic-sheets doctor`. The workbook is still produced by `openpyxl`. |
| `Excel COM automation failed … RPC_E_CALL_REJECTED` | Excel is showing a modal dialog. Close it — the agent retries automatically. |
| `Cannot overwrite …xlsx` | The workbook is open in another Excel window. Close it or pass a different `workbook_filename`. |
| `The Google Sheets or Drive API is not enabled` | Enable **both** APIs for the project that owns your credentials. |
| Spreadsheet created but you can't open it | Service accounts have a private Drive. Set `GOOGLE_SHARE_WITH_EMAIL`. |
| `Permission denied` on an existing sheet | Share that spreadsheet with the service-account email (Editor). |
| OAuth `access_denied` | Consent screen is in *Testing* — add yourself under **Audience → Test users**. |
| `Reached the 25-turn limit` | Raise `AGENT_MAX_ITERATIONS`, or split the instruction. |
| Report says truncated | Raise `AGENT_MAX_TOKENS`. |
| `RPC_E_DISCONNECTED (0x80010108)` in `pytest -m slow` | Cosmetic. Excel's `Quit()` is asynchronous, so the final COM `Release()` can land on a closed RPC channel; pytest's `faulthandler` prints it. Nothing raises, no result changes, no process is orphaned — which is why the default `excel_probe` is a registry check and the launch tests are opt-in. |

---

## Project layout

```
Technical_Assessment/
├── README.md                     · this file
├── DEMO.md                       · recording script for the demo video
├── requirements.txt              · runtime dependencies
├── requirements-dev.txt          · + pytest
├── pyproject.toml                · packaging, console script, pytest config
├── Dockerfile / docker-compose.yml
├── .env.example
├── config/tools.yaml             · tool enable/disable + retry overrides
├── credentials/                  · Google credentials (gitignored)
├── examples/prompts.md           · example prompts and expected outcomes
├── src/agentic_sheets/
│   ├── cli.py                    · run / chat / tools / doctor / sessions / serve / mcp
│   ├── config.py                 · pydantic-settings
│   ├── console.py                · Rich renderer (event-bus subscriber)
│   ├── errors.py                 · retryable vs permanent failures
│   ├── events.py                 · event bus for progress updates
│   ├── logging_setup.py          · structlog → console + JSON lines
│   ├── memory.py                 · conversation + working facts + run history
│   ├── retry.py                  · backoff policy
│   ├── agent/
│   │   ├── executor.py           · the tool-calling loop
│   │   ├── planner.py            · structured-output planning pass
│   │   └── prompts.py            · system + planner prompts, plan schema
│   ├── llm/
│   │   ├── base.py               · provider-agnostic protocol
│   │   ├── anthropic_client.py   · Claude adapter (streaming, adaptive thinking)
│   │   └── openai_compatible_client.py  · Groq / OpenAI / OpenRouter / Together / Ollama
│   ├── tools/
│   │   ├── base.py               · Tool ABC + ToolContext
│   │   ├── registry.py           · discovery, YAML config, dispatch
│   │   ├── data_tools.py         · generate CSV, convert formats, preview
│   │   ├── excel_tools.py        · COM automation + fallback + verify
│   │   └── sheets_tools.py       · Sheets/Drive API + verify
│   ├── api/
│   │   ├── server.py             · FastAPI + SSE + serves the UI
│   │   └── static/index.html     · the web UI (self-contained, no build step)
│   └── mcp_server/server.py      · MCP server
└── tests/                        · 229 offline tests + opt-in Excel tests
```

---

## Assessment requirements checklist

### Functional requirements

| Requirement | Where |
|---|---|
| Accept natural language input | `agentic-sheets run "<anything>"`, `chat`, `POST /runs` |
| Use an approved stack | Python, native function/tool calling, FastAPI, MCP, own agent framework. LLM: Groq (Llama) by default, Claude via one env var. |
| Decide which tools to execute | Planner + native tool calling; nothing is hard-coded |
| Generate a CSV automatically | `generate_employee_csv` |
| ≥ 20 rows of realistic data | Default 25; department-appropriate salaries, unique emails, 10 columns |
| Launch Microsoft Excel | `excel_import_csv` → `DispatchEx("Excel.Application")` |
| Import the CSV into Excel | Excel's own `QueryTable` text-import engine |
| Save the workbook | `SaveAs(..., FileFormat=51)` |
| Connect to Google Sheets via the API | `google-api-python-client`, Sheets v4 + Drive v3 |
| Import the same CSV into a Google Sheet | `google_sheets_import` |
| Report success/failure per step | Step-report table, structured `RunResult`, exit codes 0/1/2 |
| Handle errors gracefully | Retryable vs permanent errors, remediation hints, fallback engine, partial-success status |

### Bonus points

| Bonus | Where |
|---|---|
| Multi-step planning before execution | [`agent/planner.py`](src/agentic_sheets/agent/planner.py) — schema-constrained plan with per-step fallbacks |
| Memory / conversation history | [`memory.py`](src/agentic_sheets/memory.py) — conversation + working facts + run history, `--session/--continue`, `chat` |
| Additional formats (XLSX, CSV, ODS) | `convert_spreadsheet` |
| Retry logic for failed actions | [`retry.py`](src/agentic_sheets/retry.py) + per-tool budgets in `tools.yaml` |
| Configurable tools | [`config/tools.yaml`](config/tools.yaml) |
| MCP server integration | [`mcp_server/server.py`](src/agentic_sheets/mcp_server/server.py) + real handshake test |
| Dockerized deployment | [`Dockerfile`](Dockerfile), [`docker-compose.yml`](docker-compose.yml) |
| Unit tests | 121 offline tests + opt-in Excel integration tests |
| Structured logging | [`logging_setup.py`](src/agentic_sheets/logging_setup.py) — structlog, JSON lines, `run_id` on every line |
| Progress updates while executing | [`events.py`](src/agentic_sheets/events.py) → Rich console + SSE + [web UI](#web-ui) |

### Beyond the brief

| Extra | Where |
|---|---|
| Web UI with live progress | [`api/static/index.html`](src/agentic_sheets/api/static/index.html) — `agentic-sheets serve --open` |
| Multi-provider LLM support | Runs free on Groq; Claude/OpenAI/OpenRouter/Together/Ollama by one env var |
| `doctor` preflight command | Checks provider, key, Excel COM, Google credentials and paths independently |
| Verification tools | Both destinations are re-read after writing, so the report states *verified* success |

### Restrictions

- **One command, no further interaction.** The system prompt tells the agent the user is
  unavailable; it makes routine choices itself and only stops for genuinely unsafe or
  ambiguous cases.
- **Not a hard-coded script.** The tool list, order and arguments come from the model.
  Turn off a tool or remove Excel and the plan changes shape — see
  [`examples/prompts.md`](examples/prompts.md) for demonstrations.

---

## Verification status

What was actually exercised, as opposed to asserted. Every row below was run on this machine.

| Leg | How it was verified |
|---|---|
| **Full workflow, one command** | `COMPLETED · 6/6 steps · 7 model turns · 178s`. CSV → Excel → save → verify → Google Sheets → verify, from the assessment's own prompt, no interaction. |
| **Microsoft Excel** | Real COM automation: `engine=excel-com`, Excel 16.0 launched, `QueryTable` text import, workbook saved. Read back with `openpyxl`: 51 rows × 10 cols, typed salary, `freeze_panes=A2`, `AutoFilter=A1:J51`. No orphaned `EXCEL.EXE`. |
| **Google Sheets** | Live Sheets API v4: 50 rows / 230+ cells written, then read back — `verified: True`. Also proven to *fail* correctly: wrong expectations produce `verified: False` with both problems named. |
| **Graceful degradation** | With `GOOGLE_AUTH_MODE=disabled` and with a quota-less service account, the run reports `PARTIAL`, exit code 1, Excel steps still SUCCESS, and the Google step carries the remediation. |
| **Groq (free tier)** | Live round trip. Two real interop bugs found and fixed this way — see [What differs on a smaller open model](#what-differs-on-a-smaller-open-model). |
| **HTTP API + SSE + web UI** | Real uvicorn server: page served, `POST /runs` → 202, full SSE event stream consumed, report fetched, workbook on disk. |
| **MCP server** | Real protocol handshake against the server as a subprocess (MCP 2.x API). |
| **Test suite** | **239 passing**, 87% coverage. 237 run fully offline — no API key, no Excel, no Google credentials. |

**Not verified**, stated so you don't have to guess:

- **The Docker image has not been built.** Docker Desktop was not running on the build machine.
  The `Dockerfile` and `docker-compose.yml` are written and reviewed but unexecuted — treat them
  as untested until `docker compose up --build` succeeds for you.
- **Anthropic / OpenAI / OpenRouter / Together / Ollama** were not called live (no keys on this
  machine). They share the OpenAI-compatible adapter that Groq exercises, plus unit and
  wire-format tests asserting the exact request JSON.
- **`llama-3.1-8b-instant` does not work** for this workload — it fails to emit a parsable tool
  call even with simplified schemas. `openai/gpt-oss-120b` and `llama-3.3-70b-versatile` both do.

## Known limitations

Stated plainly rather than papered over:

1. **Real Excel automation needs Windows + desktop Excel.** COM has no Linux/macOS
   equivalent. Everywhere else `excel_import_csv` produces a genuine `.xlsx` via
   `openpyxl` and reports `excel_launched: false`; the agent never claims otherwise.
2. **A service account cannot create a spreadsheet.** Google removed free Drive storage from
   service accounts, so they cannot own Drive files; `spreadsheets.create` returns a bare
   `403` even with both APIs enabled. Use `GOOGLE_AUTH_MODE=oauth` to have the agent create
   sheets in your own Drive, or point `GOOGLE_SPREADSHEET_ID` at a sheet you own and shared
   with the service account. The agent detects this exact case and names both fixes — see
   [Google Sheets setup](#google-sheets-setup). `doctor` verifies credentials load before you
   spend a run finding out.
3. **Sheets uploads are a single `values.update` call.** Fine well past the 5,000-row cap
   on `generate_employee_csv`; a genuinely large dataset should be chunked.
4. **Run state in the HTTP API is in-process.** Restarting the server loses run history
   (session memory on disk survives). A real deployment would put runs in Redis or a DB.
5. **Groq's free tier has a 12,000 tokens-per-minute cap, and an agent loop is token-hungry.**
   A six-turn run costs roughly 20k input tokens in total, because the whole conversation is
   resent each turn. Two consecutive runs inside a minute will be rejected. The agent now
   keeps its context lean (see [Free-tier token budget](#free-tier-token-budget)) and turns
   the rejection into actionable advice rather than a raw 413, but the ceiling is real — pause
   a minute between runs, or use `--no-plan`, a larger-budget model, or `--provider anthropic`.
6. **Google Gemini has no adapter.** Its API is not OpenAI-compatible in the same way, so
   it would need a third adapter class rather than a config line.
7. **`chat` shares one process-wide settings object.** Per-run overrides in `chat` would
   need the settings threaded through rather than mutated.
