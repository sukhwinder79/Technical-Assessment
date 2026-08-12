"""The generated-files endpoints.

These let the browser show what the agent actually wrote to disk. They also
expose the filesystem to a web client, so the confinement tests below matter
more than the happy path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI is not installed.")

from fastapi.testclient import TestClient  # noqa: E402

from agentic_sheets.api import server as api_server  # noqa: E402
from agentic_sheets.config import get_settings  # noqa: E402


@pytest.fixture
def workspace(monkeypatch, tmp_path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setenv("WORKSPACE_DIR", str(ws))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "memory"))
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    get_settings(refresh=True)
    yield ws
    get_settings(refresh=True)


@pytest.fixture
def client(workspace):
    with TestClient(api_server.app) as test_client:
        yield test_client


@pytest.fixture
def sample_csv(workspace) -> Path:
    path = workspace / "employees.csv"
    path.write_text(
        "Employee ID,Name,Department,Salary\n"
        "EMP001,John Smith,Sales,65000\n"
        "EMP002,Alice Brown,HR,72000\n",
        encoding="utf-8-sig",
    )
    return path


@pytest.fixture
def sample_xlsx(workspace, sample_csv) -> Path:
    from agentic_sheets.tools.data_tools import ConvertSpreadsheetTool, read_csv

    headers, rows = read_csv(sample_csv)
    path = workspace / "employees.xlsx"
    ConvertSpreadsheetTool._write_xlsx(path, headers, rows, "Employees")
    return path


# ---- listing ---------------------------------------------------------------


def test_empty_workspace_lists_nothing(client):
    body = client.get("/files").json()
    assert body["files"] == []


def test_listing_reports_size_and_urls(client, sample_csv):
    files = client.get("/files").json()["files"]
    assert [f["name"] for f in files] == ["employees.csv"]
    entry = files[0]
    assert entry["size_bytes"] > 0
    assert entry["previewable"] is True
    assert entry["download_url"] == "/files/employees.csv"


def test_excel_lock_files_are_hidden(client, workspace, sample_csv):
    (workspace / "~$employees.xlsx").write_bytes(b"lock")
    names = [f["name"] for f in client.get("/files").json()["files"]]
    assert "~$employees.xlsx" not in names


# ---- download --------------------------------------------------------------


def test_a_generated_file_downloads(client, sample_csv):
    response = client.get("/files/employees.csv")
    assert response.status_code == 200
    assert "John Smith" in response.text


def test_downloading_a_missing_file_is_404(client):
    assert client.get("/files/nope.csv").status_code == 404


# ---- confinement (the part that matters) -----------------------------------


@pytest.mark.parametrize(
    "attack",
    [
        "../.env",
        "..%2F.env",
        "....//.env",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "..\\..\\secrets.json",
        "subdir/../../.env",
    ],
)
def test_path_traversal_cannot_escape_the_workspace(client, workspace, attack):
    """A browser must never read outside WORKSPACE_DIR."""
    outside = workspace.parent / ".env"
    outside.write_text("GROQ_API_KEY=super-secret", encoding="utf-8")

    response = client.get(f"/files/{attack}")
    assert response.status_code in (307, 404), response.status_code
    assert "super-secret" not in response.text


def test_a_file_in_a_subdirectory_is_not_served(client, workspace):
    nested = workspace / "sub"
    nested.mkdir()
    (nested / "hidden.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    # Only the workspace root is served; the basename does not exist there.
    assert client.get("/files/hidden.csv").status_code == 404


# ---- preview ---------------------------------------------------------------


def test_csv_preview_returns_rows_and_columns(client, sample_csv):
    body = client.get("/files/employees.csv/preview").json()
    assert body["columns"] == ["Employee ID", "Name", "Department", "Salary"]
    assert body["total_rows"] == 2
    assert body["rows"][0][:2] == ["EMP001", "John Smith"]
    assert body["truncated"] is False


def test_xlsx_preview_reads_the_workbook_back(client, sample_xlsx):
    body = client.get("/files/employees.xlsx/preview").json()
    assert body["columns"][0] == "Employee ID"
    assert body["total_rows"] == 2
    assert body["rows"][1][1] == "Alice Brown"


def test_preview_truncates_and_says_so(client, workspace):
    lines = ["id,name"] + [f"{i},Person {i}" for i in range(100)]
    (workspace / "big.csv").write_text("\n".join(lines), encoding="utf-8")

    body = client.get("/files/big.csv/preview?rows=10").json()
    assert len(body["rows"]) == 10
    assert body["total_rows"] == 100
    assert body["truncated"] is True


def test_preview_row_count_is_bounded(client, workspace):
    lines = ["id"] + [str(i) for i in range(500)]
    (workspace / "big.csv").write_text("\n".join(lines), encoding="utf-8")
    body = client.get("/files/big.csv/preview?rows=99999").json()
    assert len(body["rows"]) <= 200


def test_ods_preview_works(client, workspace, sample_csv):
    """Regression: /files advertised .ods as previewable but /preview 415'd,
    so the UI showed a View button that always failed."""
    from agentic_sheets.tools.data_tools import ConvertSpreadsheetTool, read_csv

    headers, rows = read_csv(sample_csv)
    ConvertSpreadsheetTool._write_ods(workspace / "employees.ods", headers, rows, "Employees")

    listed = next(f for f in client.get("/files").json()["files"] if f["name"].endswith(".ods"))
    assert listed["previewable"] is True, "listing promises a preview"

    body = client.get("/files/employees.ods/preview").json()
    assert body["columns"] == ["Employee ID", "Name", "Department", "Salary"]
    assert body["total_rows"] == 2
    assert body["rows"][0][1] == "John Smith"


def test_every_previewable_extension_really_previews(client, workspace, sample_csv, sample_xlsx):
    """The listing must not promise a preview the endpoint cannot deliver."""
    from agentic_sheets.tools.data_tools import ConvertSpreadsheetTool, read_csv

    headers, rows = read_csv(sample_csv)
    ConvertSpreadsheetTool._write_ods(workspace / "employees.ods", headers, rows, "Employees")

    for entry in client.get("/files").json()["files"]:
        if not entry["previewable"]:
            continue
        response = client.get(entry["preview_url"])
        assert response.status_code == 200, f"{entry['name']} is listed previewable but returned {response.status_code}"


def test_preview_rejects_an_unsupported_format(client, workspace):
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")
    response = client.get("/files/notes.txt/preview")
    assert response.status_code == 415
    assert "Download it instead" in response.json()["detail"]


def test_dates_render_without_a_midnight_component(client, workspace):
    """Excel turns ISO dates into datetimes; the preview must not show 00:00:00."""
    import datetime as dt

    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Name", "Hire Date"])
    sheet.append(["Ann", dt.datetime(2024, 6, 25)])
    path = workspace / "dates.xlsx"
    workbook.save(path)

    body = client.get("/files/dates.xlsx/preview").json()
    assert body["rows"][0][1] == "2024-06-25"


# ---- the UI wires it up ----------------------------------------------------


def test_the_ui_uses_the_file_endpoints(client):
    page = client.get("/").text
    assert "/files" in page
    assert "/preview" in page
    assert "Download" in page and "View data" in page


def test_the_ui_browses_the_workspace_without_a_run(client):
    """Inspecting earlier output must not require spending API tokens."""
    page = client.get("/").text
    assert "Workspace files" in page
    assert "loadWorkspaceFiles" in page
    # It is loaded on page load, not only after a run finishes.
    assert "loadWorkspaceFiles();" in page


def test_the_ui_warns_about_the_model_that_cannot_tool_call(client):
    page = client.get("/").text
    assert "llama-3.1-8b-instant" in page
    assert "cannot emit a parsable tool call" in page
