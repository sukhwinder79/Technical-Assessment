# Example prompts

Every prompt below runs against the **same** toolbox. Nothing about the sequence is
hard-coded — the agent plans a different path for each one. Each entry lists what the
agent is expected to do, which is also how to sanity-check a run.

---

## 1. The assessment prompt

```powershell
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets."
```

Also the default, so this is equivalent:

```powershell
agentic-sheets run
```

**Expected plan:** generate CSV → import into Excel → verify workbook → upload to Google
Sheets → verify sheet.

**Expected result:** `COMPLETED`, 5/5 steps, with `workspace/employees.csv`,
`workspace/employees.xlsx` and a `docs.google.com/spreadsheets/d/…` URL in the report.

---

## 2. The variant in the brief

```powershell
agentic-sheets run "Create an employee CSV and import it into Excel and Google Sheets."
```

Same outcome. Worth including in the demo to show the agent is parsing intent, not
matching a fixed string.

---

## 3. Explicit row count, sheet title, and verification

```powershell
agentic-sheets run "Generate 50 employee records with realistic salaries, open them in Excel, save the workbook, then upload the same data to a Google Sheet called 'Q3 Headcount' and confirm both imports."
```

**What to look for:** `row_count=50` in the CSV call, `spreadsheet_title="Q3 Headcount"`
passed through to `google_sheets_import`, and both verify tools called exactly once each.

---

## 4. Multiple formats (bonus: XLSX / CSV / ODS)

```powershell
agentic-sheets run "I need sample HR data in three formats - CSV, XLSX and ODS - plus a Google Sheet. Verify every destination and tell me the row counts."
```

**What to look for:** `convert_spreadsheet` called twice (once for `ods`, and either
`excel_import_csv` or `convert_spreadsheet` for the `xlsx`), and row counts quoted from
tool results rather than assumed.

---

## 5. Excel only — partial scope

```powershell
agentic-sheets run "Build a 25-row employee dataset, import it into Excel only (skip Google Sheets), and confirm the workbook has the right headers."
```

**What to look for:** `google_sheets_import` is **never called**. The agent should pass
`expected_columns` to `excel_verify_workbook` rather than eyeballing the header.

---

## 6. Reproducible data

```powershell
agentic-sheets run "Use reproducible data (seed 42) so I get the same 30 rows every time, then load it into Excel and Google Sheets."
```

**What to look for:** `seed=42` in the tool call. Run it twice — the CSV bytes should be
identical.

---

## 7. Memory across runs (bonus: conversation history)

```powershell
agentic-sheets run "Generate 40 employee records and load them into Excel" --session demo
agentic-sheets run "Now export that same data to ODS as well" --session demo --continue
```

**What to look for:** the second run does **not** regenerate the CSV. It resolves "that
same data" from working memory (`last_csv_path`) and calls `convert_spreadsheet` directly.
Inspect the memory with:

```powershell
agentic-sheets sessions --show demo
```

---

## 8. Interactive mode

```powershell
agentic-sheets chat --session demo
```

```text
you › create 20 employee records and put them in Excel
you › how many rows did that have?
you › now also push it to Google Sheets
you › exit
```

The middle question is answered from memory with no tool call at all — evidence that
history is real rather than decorative.

---

## 9. Existing spreadsheet instead of a new one

```powershell
agentic-sheets run "Generate 30 employee records and write them into the 'Employees' tab of spreadsheet 1AbCdEfGhIjKlMnOpQrStUvWxYz, replacing whatever is there."
```

**Prerequisite:** with a service account, share that spreadsheet with the service-account
email (Editor). Otherwise expect the 403 → *"share the target spreadsheet with the service
account"* remediation, which is itself a good error-handling demo.

---

## 10. Graceful degradation — tool disabled

Edit `config/tools.yaml`:

```yaml
tools:
  excel_import_csv:
    enabled: false
```

Then run the standard prompt:

```powershell
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets."
```

**Expected:** the Excel tool is no longer in the toolbox at all, so the agent plans around
it — typically `convert_spreadsheet(target_format='xlsx')` — and reports that the Excel
**application** was not launched. This is the clearest single demonstration that the
workflow is model-driven rather than scripted. Remember to re-enable it afterwards.

---

## 11. Graceful degradation — Google Sheets not configured

```powershell
$env:GOOGLE_AUTH_MODE="disabled"
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets."
```

**Expected:** `PARTIAL` (exit code 1). Excel steps succeed; the Google Sheets step is
reported as skipped with the reason and the exact fix.

---

## 12. Error recovery — missing credentials

```powershell
$env:GOOGLE_AUTH_MODE="service_account"
Rename-Item credentials\service_account.json credentials\service_account.json.bak
agentic-sheets run "Create an employee CSV and upload it to Google Sheets."
```

**Expected:** the tool fails **once** (not three times — a missing file is not retryable),
the model receives the `remediation` text and surfaces it in the report instead of looping.
Restore the file afterwards.

---

## 13. Machine-readable output for CI

```powershell
agentic-sheets run "Create an employee CSV and import it into Excel" --json-out reports/run.json
```

```powershell
Get-Content reports/run.json | ConvertFrom-Json | Select-Object status, duration_s
```

Exit codes: `0` completed · `1` partial · `2` failed.

---

## 14. Via the HTTP API

```bash
curl -s -X POST localhost:8000/runs/sync \
  -H 'content-type: application/json' \
  -d '{"instruction":"Create a sample employee CSV and import it into Excel and Google Sheets.","effort":"medium"}' \
  | jq '.result.steps[] | {tool, status, summary}'
```

---

## 15. Cost / latency control

```powershell
# Fewest API calls: skip the planning pass (one call less per run)
agentic-sheets run "Create an employee CSV and import it into Excel" --no-plan

# Fastest Groq model (weaker at long tool loops)
agentic-sheets run "..." --model llama-3.1-8b-instant

# Strongest reasoning on the free tier
agentic-sheets run "..." --model openai/gpt-oss-120b

# On Claude, `--effort` tunes reasoning depth (Anthropic only)
agentic-sheets run "..." --provider anthropic --effort xhigh
```

---

## 16. Same prompt, different LLM

Useful for the demo: the identical instruction on three providers, proving the agent logic
is provider-independent.

```powershell
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets." --provider groq
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets." --provider anthropic
agentic-sheets run "Create a sample employee CSV and import it into Excel and Google Sheets." --provider ollama --model qwen2.5:14b
```

**What to look for:** the plan and the tool sequence may differ in detail — different models
make different choices — but the artifacts and the final per-step report are equivalent.
The last one needs no internet at all beyond the Google Sheets call.
