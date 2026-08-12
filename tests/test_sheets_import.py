"""Google Sheets import/verify logic, with the API client faked.

`_build_services` is the seam: everything above it (create-vs-reuse, adding a
worksheet, clearing, the `values.update` payload, header formatting, Drive
sharing, working memory) is ours and worth testing. Everything below it is
Google's.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_sheets.errors import ToolError
from agentic_sheets.tools.data_tools import GenerateEmployeeCsvTool
from agentic_sheets.tools.sheets_tools import GoogleSheetsImportTool, GoogleSheetsVerifyTool


# ---- fakes -----------------------------------------------------------------


class FakeExec:
    def __init__(self, result, recorder=None, label=None, kwargs=None) -> None:
        self._result = result
        self._recorder = recorder
        self._label = label
        self._kwargs = kwargs or {}

    def execute(self):
        if self._recorder is not None:
            self._recorder.append((self._label, self._kwargs))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeValues:
    def __init__(self, parent) -> None:
        self.parent = parent

    def update(self, **kwargs):
        self.parent.calls.append(("values.update", kwargs))
        rows = kwargs["body"]["values"]
        self.parent.written = rows
        return FakeExec(
            {
                "updatedCells": sum(len(row) for row in rows),
                "updatedRange": f"{kwargs['range']}:J{len(rows)}",
            }
        )

    def clear(self, **kwargs):
        self.parent.calls.append(("values.clear", kwargs))
        return FakeExec({})

    def get(self, **kwargs):
        self.parent.calls.append(("values.get", kwargs))
        if self.parent.read_error is not None:
            return FakeExec(self.parent.read_error)
        return FakeExec({"values": self.parent.written})


class FakeSpreadsheets:
    def __init__(self, parent) -> None:
        self.parent = parent

    def create(self, **kwargs):
        self.parent.calls.append(("create", kwargs))
        self.parent.created_title = kwargs["body"]["properties"]["title"]
        return FakeExec(
            {
                "spreadsheetId": "sheet-new",
                "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet-new",
            }
        )

    def get(self, **kwargs):
        self.parent.calls.append(("get", kwargs))
        return FakeExec(
            {
                "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{kwargs['spreadsheetId']}",
                "sheets": [{"properties": {"title": title, "sheetId": index}}
                           for index, title in enumerate(self.parent.existing_tabs)],
            }
        )

    def batchUpdate(self, **kwargs):  # noqa: N802 - mirrors the Google client
        self.parent.calls.append(("batchUpdate", kwargs))
        requests = kwargs["body"]["requests"]
        if requests and "addSheet" in requests[0]:
            title = requests[0]["addSheet"]["properties"]["title"]
            self.parent.existing_tabs.append(title)
            return FakeExec({"replies": [{"addSheet": {"properties": {"sheetId": 99, "title": title}}}]})
        return FakeExec({"replies": []})

    def values(self):
        return FakeValues(self.parent)


class FakeSheetsService:
    def __init__(self, existing_tabs=("Sheet1",)) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.existing_tabs = list(existing_tabs)
        self.written: list[list] = []
        self.created_title: str | None = None
        self.read_error: Exception | None = None

    def spreadsheets(self):
        return FakeSpreadsheets(self)

    def method_names(self) -> list[str]:
        return [name for name, _ in self.calls]


class FakePermissions:
    def __init__(self, parent) -> None:
        self.parent = parent

    def create(self, **kwargs):
        self.parent.shared.append(kwargs)
        if self.parent.share_error is not None:
            return FakeExec(self.parent.share_error)
        return FakeExec({"id": "perm-1"})


class FakeDriveService:
    def __init__(self) -> None:
        self.shared: list[dict] = []
        self.share_error: Exception | None = None

    def permissions(self):
        return FakePermissions(self)


@pytest.fixture
def csv_path(ctx) -> str:
    return GenerateEmployeeCsvTool().run(
        GenerateEmployeeCsvTool.args_model(row_count=22, seed=3), ctx
    )["csv_path"]


@pytest.fixture
def services(monkeypatch):
    sheets, drive = FakeSheetsService(), FakeDriveService()
    monkeypatch.setattr(
        "agentic_sheets.tools.sheets_tools._build_services",
        lambda settings: (sheets, drive, "agent@project.iam.gserviceaccount.com"),
    )
    return sheets, drive


# ---- creating a new spreadsheet --------------------------------------------


def test_creates_shares_and_writes_every_row(ctx, csv_path, services):
    sheets, drive = services
    ctx.settings.google_share_with_email = "me@example.com"

    tool = GoogleSheetsImportTool()
    result = tool.run(
        tool.args_model(csv_path=csv_path, spreadsheet_title="Q3 Headcount", worksheet_name="Employees"),
        ctx,
    )

    assert result["ok"] is True
    assert result["created_new_spreadsheet"] is True
    assert result["spreadsheet_id"] == "sheet-new"
    assert result["spreadsheet_url"].endswith("sheet-new")
    assert sheets.created_title == "Q3 Headcount"

    # Header + 22 data rows, and numbers stayed numbers.
    assert result["data_rows"] == 22
    assert len(sheets.written) == 23
    assert sheets.written[0][0] == "Employee ID"
    salary_column = sheets.written[0].index("Salary")
    assert isinstance(sheets.written[1][salary_column], int)

    # USER_ENTERED so Sheets parses dates and numbers rather than storing text.
    update = next(kwargs for name, kwargs in sheets.calls if name == "values.update")
    assert update["valueInputOption"] == "USER_ENTERED"
    assert update["range"] == "'Employees'!A1"

    assert drive.shared[0]["body"]["emailAddress"] == "me@example.com"
    assert drive.shared[0]["body"]["role"] == "writer"
    assert result["shared_with"] == "me@example.com"

    assert ctx.memory.recall("last_spreadsheet_id") == "sheet-new"
    assert ctx.memory.recall("last_spreadsheet_url").endswith("sheet-new")


def test_adds_the_worksheet_when_the_tab_is_missing(ctx, csv_path, services):
    sheets, _drive = services
    tool = GoogleSheetsImportTool()
    tool.run(tool.args_model(csv_path=csv_path, worksheet_name="Employees"), ctx)

    add_sheet = [
        kwargs for name, kwargs in sheets.calls
        if name == "batchUpdate" and "addSheet" in kwargs["body"]["requests"][0]
    ]
    assert len(add_sheet) == 1
    assert add_sheet[0]["body"]["requests"][0]["addSheet"]["properties"]["title"] == "Employees"


def test_reuses_an_existing_tab_without_adding_it(ctx, csv_path, monkeypatch):
    sheets, drive = FakeSheetsService(existing_tabs=("Employees",)), FakeDriveService()
    monkeypatch.setattr(
        "agentic_sheets.tools.sheets_tools._build_services", lambda settings: (sheets, drive, None)
    )
    tool = GoogleSheetsImportTool()
    tool.run(tool.args_model(csv_path=csv_path, worksheet_name="Employees"), ctx)

    assert not any(
        name == "batchUpdate" and "addSheet" in kwargs["body"]["requests"][0]
        for name, kwargs in sheets.calls
    )


def test_clear_existing_can_be_switched_off(ctx, csv_path, services):
    sheets, _drive = services
    tool = GoogleSheetsImportTool()
    tool.run(tool.args_model(csv_path=csv_path, clear_existing=False), ctx)
    assert "values.clear" not in sheets.method_names()


def test_header_formatting_is_applied(ctx, csv_path, services):
    sheets, _drive = services
    tool = GoogleSheetsImportTool()
    tool.run(tool.args_model(csv_path=csv_path), ctx)

    format_requests = [
        request
        for name, kwargs in sheets.calls
        if name == "batchUpdate"
        for request in kwargs["body"]["requests"]
    ]
    kinds = {key for request in format_requests for key in request}
    assert {"repeatCell", "updateSheetProperties", "autoResizeDimensions"} <= kinds


def test_formatting_failures_never_fail_the_import(ctx, csv_path, monkeypatch, services):
    sheets, _drive = services

    original = sheets.spreadsheets

    def spreadsheets():
        obj = original()
        real_batch = obj.batchUpdate

        def batchUpdate(**kwargs):  # noqa: N802
            requests = kwargs["body"]["requests"]
            if requests and "repeatCell" in requests[0]:
                return FakeExec(RuntimeError("formatting blew up"))
            return real_batch(**kwargs)

        obj.batchUpdate = batchUpdate
        return obj

    sheets.spreadsheets = spreadsheets

    tool = GoogleSheetsImportTool()
    result = tool.run(tool.args_model(csv_path=csv_path), ctx)
    assert result["ok"] is True  # cosmetic failure, data still landed


# ---- reusing an existing spreadsheet ---------------------------------------


def test_writes_into_an_explicit_spreadsheet_id(ctx, csv_path, services):
    sheets, drive = services
    tool = GoogleSheetsImportTool()
    result = tool.run(tool.args_model(csv_path=csv_path, spreadsheet_id="existing-123"), ctx)

    assert result["created_new_spreadsheet"] is False
    assert result["spreadsheet_id"] == "existing-123"
    assert "create" not in sheets.method_names()
    assert drive.shared == []  # never re-share a sheet we did not create


def test_settings_spreadsheet_id_is_used_when_no_argument_is_given(ctx, csv_path, services):
    ctx.settings.google_spreadsheet_id = "from-env-456"
    tool = GoogleSheetsImportTool()
    result = tool.run(tool.args_model(csv_path=csv_path), ctx)
    assert result["spreadsheet_id"] == "from-env-456"


# ---- sharing ---------------------------------------------------------------


def test_a_failed_share_warns_but_keeps_the_data(ctx, csv_path, services):
    sheets, drive = services
    drive.share_error = RuntimeError("no permission to share")
    ctx.settings.google_share_with_email = "me@example.com"

    tool = GoogleSheetsImportTool()
    result = tool.run(tool.args_model(csv_path=csv_path), ctx)

    assert result["ok"] is True
    assert result["shared_with"] is None
    assert "could not be shared" in result["warning"]


def test_no_share_target_means_no_drive_call(ctx, csv_path, services):
    _sheets, drive = services
    ctx.settings.google_share_with_email = None
    tool = GoogleSheetsImportTool()
    result = tool.run(tool.args_model(csv_path=csv_path), ctx)
    assert drive.shared == []
    assert "warning" not in result


# ---- verification ----------------------------------------------------------


def test_verify_confirms_rows_and_headers(ctx, csv_path, services):
    sheets, _drive = services
    importer = GoogleSheetsImportTool()
    importer.run(importer.args_model(csv_path=csv_path, worksheet_name="Employees"), ctx)

    verifier = GoogleSheetsVerifyTool()
    result = verifier.run(
        verifier.args_model(
            spreadsheet_id="sheet-new",
            worksheet_name="Employees",
            expected_row_count=22,
            expected_columns=ctx.memory.recall("last_csv_columns"),
        ),
        ctx,
    )

    assert result["verified"] is True
    assert result["data_row_count"] == 22
    assert result["problems"] == []
    assert result["checks"]["row_count"]["passed"] is True
    assert result["checks"]["columns"]["passed"] is True


def test_verify_reports_a_mismatch_instead_of_passing(ctx, csv_path, services):
    importer = GoogleSheetsImportTool()
    importer.run(importer.args_model(csv_path=csv_path), ctx)

    verifier = GoogleSheetsVerifyTool()
    result = verifier.run(
        verifier.args_model(spreadsheet_id="sheet-new", expected_row_count=999), ctx
    )
    assert result["verified"] is False
    assert "999" in result["problems"][0]


def test_verify_on_an_empty_worksheet_is_an_error(ctx, services):
    sheets, _drive = services
    sheets.written = []
    verifier = GoogleSheetsVerifyTool()
    with pytest.raises(ToolError) as exc:
        verifier.run(verifier.args_model(spreadsheet_id="sheet-new"), ctx)
    assert "empty" in exc.value.message.lower()
