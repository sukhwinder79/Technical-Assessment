"""Excel tools.

The COM path needs a real Windows + Excel host, so it is marked and skipped
elsewhere. The fallback path, the probe and the verifier run everywhere — which
is exactly the point: the agent must behave sensibly with and without Excel.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentic_sheets.errors import ToolError
from agentic_sheets.tools.base import ToolContext
from agentic_sheets.tools.data_tools import GenerateEmployeeCsvTool
from agentic_sheets.tools.excel_tools import (
    ExcelImportCsvTool,
    ExcelProbeTool,
    ExcelVerifyWorkbookTool,
    _com_available,
)

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32" or not _com_available()[0],
    reason="Requires Windows with Microsoft Excel and pywin32.",
)


@pytest.fixture
def sample_csv(ctx: ToolContext) -> Path:
    tool = GenerateEmployeeCsvTool()
    return Path(tool.run(tool.args_model(row_count=25, seed=4), ctx)["csv_path"])


# ---- probe -----------------------------------------------------------------


def test_probe_always_answers_and_never_raises(ctx: ToolContext):
    result = ExcelProbeTool().run(ExcelProbeTool.args_model(), ctx)
    assert result["ok"] is True
    assert isinstance(result["excel_available"], bool)
    assert result["reason"]
    if not result["excel_available"]:
        assert "fallback" in result  # the model is told what to do instead


def test_probe_records_availability_in_working_memory(ctx: ToolContext):
    ExcelProbeTool().run(ExcelProbeTool.args_model(), ctx)
    assert ctx.memory.recall("excel_available") is not None


def test_probe_on_non_windows_reports_the_fallback(ctx: ToolContext, monkeypatch):
    monkeypatch.setattr(
        "agentic_sheets.tools.excel_tools._com_available", lambda: (False, "Simulated: linux host.")
    )
    result = ExcelProbeTool().run(ExcelProbeTool.args_model(), ctx)
    assert result["excel_available"] is False
    assert "convert_spreadsheet" in result["fallback"]


# ---- import: fallback path (runs everywhere) -------------------------------


def test_fallback_writes_a_real_workbook_and_is_honest_about_it(
    ctx: ToolContext, sample_csv: Path, monkeypatch
):
    monkeypatch.setattr(
        "agentic_sheets.tools.excel_tools._com_available",
        lambda: (False, "Simulated: not running on Windows."),
    )

    tool = ExcelImportCsvTool()
    result = tool.run(
        tool.args_model(csv_path=str(sample_csv), workbook_filename="fallback.xlsx"), ctx
    )

    assert result["ok"] is True
    assert result["engine"] == "openpyxl"
    assert result["excel_launched"] is False          # never claims Excel was opened
    assert "note" in result
    assert result["data_rows"] == 25
    assert Path(result["workbook_path"]).exists()


def test_fallback_result_is_verifiable(ctx: ToolContext, sample_csv: Path, monkeypatch):
    monkeypatch.setattr(
        "agentic_sheets.tools.excel_tools._com_available", lambda: (False, "Simulated.")
    )
    imported = ExcelImportCsvTool().run(
        ExcelImportCsvTool.args_model(csv_path=str(sample_csv)), ctx
    )

    verifier = ExcelVerifyWorkbookTool()
    verified = verifier.run(
        verifier.args_model(
            workbook_path=imported["workbook_path"],
            expected_row_count=25,
            expected_columns=ctx.memory.recall("last_csv_columns"),
        ),
        ctx,
    )
    assert verified["verified"] is True
    assert verified["data_row_count"] == 25
    assert verified["problems"] == []


def test_the_model_cannot_hide_the_excel_window(ctx: ToolContext):
    """Window visibility is the operator's choice, not the agent's.

    When `visible`/`keep_open` were tool arguments, the model routinely chose
    `false` for both, silently overriding EXCEL_VISIBLE/EXCEL_KEEP_OPEN and
    hiding the window a demo exists to show. They must not be in the schema.
    """
    schema_properties = set(ExcelImportCsvTool.args_model.model_fields)
    assert "visible" not in schema_properties
    assert "keep_open" not in schema_properties

    # And an attempt to smuggle them in is simply ignored by validation.
    args = ExcelImportCsvTool.args_model.model_validate(
        {"csv_path": "x.csv", "visible": False, "keep_open": False}
    )
    assert not hasattr(args, "visible")


def test_settings_control_visibility(ctx: ToolContext, sample_csv: Path, monkeypatch):
    monkeypatch.setattr(
        "agentic_sheets.tools.excel_tools._com_available", lambda: (False, "Simulated.")
    )
    ctx.settings.excel_visible = True
    ctx.settings.excel_keep_open = True

    tool = ExcelImportCsvTool()
    # The fallback engine reports no window, but the COM path reads the same
    # two settings — proven by the visibility assertions in the COM test.
    result = tool.run(tool.args_model(csv_path=str(sample_csv)), ctx)
    assert result["excel_launched"] is False


def test_import_rejects_a_missing_csv(ctx: ToolContext):
    tool = ExcelImportCsvTool()
    with pytest.raises(ToolError) as exc:
        tool.run(tool.args_model(csv_path="does-not-exist.csv"), ctx)
    assert not exc.value.retryable


def test_workbook_extension_is_normalised(ctx: ToolContext, sample_csv: Path, monkeypatch):
    monkeypatch.setattr(
        "agentic_sheets.tools.excel_tools._com_available", lambda: (False, "Simulated.")
    )
    tool = ExcelImportCsvTool()
    result = tool.run(
        tool.args_model(csv_path=str(sample_csv), workbook_filename="report.txt"), ctx
    )
    assert Path(result["workbook_path"]).suffix == ".xlsx"


# ---- verification ----------------------------------------------------------


def test_verifier_reports_a_row_count_mismatch_rather_than_passing(
    ctx: ToolContext, sample_csv: Path, monkeypatch
):
    monkeypatch.setattr(
        "agentic_sheets.tools.excel_tools._com_available", lambda: (False, "Simulated.")
    )
    imported = ExcelImportCsvTool().run(
        ExcelImportCsvTool.args_model(csv_path=str(sample_csv)), ctx
    )

    verifier = ExcelVerifyWorkbookTool()
    result = verifier.run(
        verifier.args_model(workbook_path=imported["workbook_path"], expected_row_count=999), ctx
    )
    assert result["verified"] is False
    assert result["checks"]["row_count"]["passed"] is False
    assert "999" in result["problems"][0]


def test_verifier_detects_wrong_headers(ctx: ToolContext, sample_csv: Path, monkeypatch):
    monkeypatch.setattr(
        "agentic_sheets.tools.excel_tools._com_available", lambda: (False, "Simulated.")
    )
    imported = ExcelImportCsvTool().run(
        ExcelImportCsvTool.args_model(csv_path=str(sample_csv)), ctx
    )
    verifier = ExcelVerifyWorkbookTool()
    result = verifier.run(
        verifier.args_model(
            workbook_path=imported["workbook_path"], expected_columns=["Wrong", "Headers"]
        ),
        ctx,
    )
    assert result["verified"] is False
    assert result["checks"]["columns"]["passed"] is False


def test_verifier_rejects_a_missing_workbook(ctx: ToolContext):
    verifier = ExcelVerifyWorkbookTool()
    with pytest.raises(ToolError):
        verifier.run(verifier.args_model(workbook_path="ghost.xlsx"), ctx)


# ---- real Excel (Windows + Excel only) -------------------------------------


@WINDOWS_ONLY
@pytest.mark.slow
def test_real_excel_import_end_to_end(ctx: ToolContext, sample_csv: Path):
    """Opt-in: `pytest -m slow`. Launches the real Excel application.

    Excluded from the default run for two reasons: it needs Excel installed, and
    tearing an out-of-process COM server down is inherently racy — `Quit()`
    returns before Excel has finished exiting, so the final interface `Release()`
    can land on a closed RPC channel. That surfaces as a Windows structured
    exception (RPC_E_DISCONNECTED, 0x80010108) which pytest's faulthandler
    prints to stderr. It raises nothing, changes no result, and leaves no
    orphaned process — but it makes a clean test run look alarming.
    """
    # Visibility comes from settings, not from tool arguments.
    ctx.settings.excel_visible = False
    ctx.settings.excel_keep_open = False

    tool = ExcelImportCsvTool()
    result = tool.run(
        tool.args_model(csv_path=str(sample_csv), workbook_filename="com_import.xlsx"), ctx
    )

    assert result["engine"] == "excel-com"
    assert result["excel_launched"] is True
    assert result["data_rows"] == 25
    assert Path(result["workbook_path"]).exists()

    verifier = ExcelVerifyWorkbookTool()
    verified = verifier.run(
        verifier.args_model(workbook_path=result["workbook_path"], expected_row_count=25), ctx
    )
    assert verified["verified"] is True


@WINDOWS_ONLY
def test_real_excel_registration_probe_reports_a_version(ctx: ToolContext):
    result = ExcelProbeTool().run(ExcelProbeTool.args_model(), ctx)
    assert result["excel_available"] is True
    assert result["excel_version"]
    assert result["check"] == "COM registration"


@WINDOWS_ONLY
@pytest.mark.slow
def test_real_excel_deep_probe_launches_and_quits(ctx: ToolContext):
    """Opt-in: `pytest -m slow`.

    Excluded from the default run because Excel's `Quit()` starts an async
    teardown, so releasing the interface pointer afterwards can occasionally
    emit an RPC_E_DISCONNECTED process fault. It is harmless — the probe still
    returns the right answer — but it clutters a clean test run, which is why
    the default probe is the registry check.
    """
    result = ExcelProbeTool().run(ExcelProbeTool.args_model(deep_check=True), ctx)
    assert result["excel_available"] is True
    assert result["check"] == "launched Excel"
