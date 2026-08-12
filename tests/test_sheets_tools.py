"""Google Sheets tools — credential handling and error translation.

No network calls: the value being tested is that a raw Google `HttpError` is
turned into a message the model (and the user) can act on.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_sheets.errors import ToolError
from agentic_sheets.tools.sheets_tools import (
    GoogleSheetsImportTool,
    _load_credentials,
    _translate_http_error,
)


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "error"


def http_error(status: int, message: str = "boom"):
    from googleapiclient.errors import HttpError

    return HttpError(FakeResponse(status), message.encode("utf-8"))


# ---- credential handling ---------------------------------------------------


def test_disabled_mode_tells_the_agent_to_skip_the_step(settings):
    settings.google_auth_mode = "disabled"
    with pytest.raises(ToolError) as exc:
        _load_credentials(settings)
    assert "skipped" in (exc.value.remediation or "").lower()
    assert not exc.value.retryable


def test_missing_service_account_file_gives_setup_instructions(settings, tmp_path: Path):
    settings.google_auth_mode = "service_account"
    settings.google_credentials_file = tmp_path / "absent.json"
    with pytest.raises(ToolError) as exc:
        _load_credentials(settings)
    assert "not found" in exc.value.message
    assert "Sheets API" in (exc.value.remediation or "")


def test_corrupt_service_account_file_is_reported_clearly(settings, tmp_path: Path):
    key = tmp_path / "sa.json"
    key.write_text("{ not valid json", encoding="utf-8")
    settings.google_auth_mode = "service_account"
    settings.google_credentials_file = key
    with pytest.raises(ToolError) as exc:
        _load_credentials(settings)
    assert "invalid" in exc.value.message.lower()


def test_missing_oauth_client_file_gives_setup_instructions(settings, tmp_path: Path):
    settings.google_auth_mode = "oauth"
    settings.google_oauth_client_file = tmp_path / "absent.json"
    settings.google_token_file = tmp_path / "token.json"
    with pytest.raises(ToolError) as exc:
        _load_credentials(settings)
    assert "Desktop app" in (exc.value.remediation or "")


# ---- error translation -----------------------------------------------------


def test_api_not_enabled_403_names_the_exact_fix():
    error = _translate_http_error(
        http_error(403, "Google Sheets API has not been used in project 123 before"),
        "creating the spreadsheet",
    )
    assert not error.retryable
    assert "not enabled" in error.message
    assert "APIs & Services" in (error.remediation or "")


def test_plain_403_suggests_sharing_with_the_service_account():
    error = _translate_http_error(http_error(403, "The caller does not have permission"), "writing")
    assert not error.retryable
    assert "share" in (error.remediation or "").lower()


def test_404_points_at_the_spreadsheet_id():
    error = _translate_http_error(http_error(404, "Requested entity was not found"), "writing")
    assert not error.retryable
    assert "SPREADSHEET_ID" in (error.remediation or "").upper()


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_statuses_are_retryable(status):
    assert _translate_http_error(http_error(status), "writing").retryable is True


@pytest.mark.parametrize("status", [400, 403, 404])
def test_client_errors_are_not_retryable(status):
    assert _translate_http_error(http_error(status), "writing").retryable is False


def test_non_http_exceptions_are_treated_as_transient():
    error = _translate_http_error(ConnectionResetError("socket closed"), "writing")
    assert error.retryable is True


def test_googles_own_reason_is_surfaced_not_swallowed():
    """A generic "permission denied" sends people hunting the wrong problem."""
    from agentic_sheets.tools.sheets_tools import _google_reason

    exc = http_error(403, json.dumps({"error": {"code": 403, "message": "The caller does not have permission"}}))
    assert "caller does not have permission" in _google_reason(exc)

    # Non-JSON bodies must not raise.
    assert _google_reason(http_error(500, "plain text")) is not None


def test_a_403_includes_googles_message_and_the_create_caveat():
    exc = http_error(403, json.dumps({"error": {"message": "The caller does not have permission"}}))
    error = _translate_http_error(exc, "writing")
    assert "caller does not have permission" in error.message
    assert "cannot own" in (error.remediation or "").lower()


# ---- the service-account storage limitation --------------------------------


class _Drive:
    """Minimal Drive stub exposing about().get(...).execute()."""

    def __init__(self, limit: str | None, explode: bool = False) -> None:
        self._limit = limit
        self._explode = explode

    def about(self):
        return self

    def get(self, **_kwargs):
        return self

    def execute(self):
        if self._explode:
            raise RuntimeError("drive unreachable")
        return {"storageQuota": {}} if self._limit is None else {"storageQuota": {"limit": self._limit}}


@pytest.mark.parametrize(
    ("limit", "expected"),
    [("0", True), ("15000000000", False), (None, False)],
)
def test_zero_storage_quota_identifies_the_limitation(limit, expected):
    from agentic_sheets.tools.sheets_tools import _service_account_cannot_own_files

    assert _service_account_cannot_own_files(_Drive(limit)) is expected


def test_an_unreachable_drive_does_not_break_the_diagnosis():
    from agentic_sheets.tools.sheets_tools import _service_account_cannot_own_files

    assert _service_account_cannot_own_files(_Drive("0", explode=True)) is False


def test_create_failure_on_a_quota_less_account_names_both_fixes(ctx, monkeypatch):
    """The bare 403 must become an explanation, with the SA address filled in."""
    from agentic_sheets.tools.data_tools import GenerateEmployeeCsvTool
    from agentic_sheets.tools.sheets_tools import GoogleSheetsImportTool

    csv_path = GenerateEmployeeCsvTool().run(GenerateEmployeeCsvTool.args_model(row_count=3), ctx)["csv_path"]

    class _Sheets:
        def spreadsheets(self):
            return self

        def create(self, **_kwargs):
            return self

        def execute(self):
            raise http_error(403, json.dumps({"error": {"message": "The caller does not have permission"}}))

    monkeypatch.setattr(
        "agentic_sheets.tools.sheets_tools._build_services",
        lambda settings: (_Sheets(), _Drive("0"), "bot@proj.iam.gserviceaccount.com"),
    )
    ctx.settings.google_auth_mode = "service_account"
    ctx.settings.google_spreadsheet_id = None

    tool = GoogleSheetsImportTool()
    with pytest.raises(ToolError) as exc:
        tool.run(tool.args_model(csv_path=csv_path), ctx)

    assert "no Drive storage quota" in exc.value.message
    remediation = exc.value.remediation or ""
    assert "bot@proj.iam.gserviceaccount.com" in remediation   # the actual address, not a placeholder
    assert "GOOGLE_SPREADSHEET_ID" in remediation              # fix (a)
    assert "GOOGLE_AUTH_MODE=oauth" in remediation             # fix (b)
    assert exc.value.retryable is False                        # waiting will never help


# ---- tool wiring -----------------------------------------------------------


def test_import_tool_fails_before_any_network_call_when_disabled(ctx, tmp_path: Path):
    from agentic_sheets.tools.data_tools import GenerateEmployeeCsvTool

    csv_path = GenerateEmployeeCsvTool().run(
        GenerateEmployeeCsvTool.args_model(row_count=5), ctx
    )["csv_path"]

    ctx.settings.google_auth_mode = "disabled"
    tool = GoogleSheetsImportTool()
    with pytest.raises(ToolError) as exc:
        tool.run(tool.args_model(csv_path=csv_path), ctx)
    assert "disabled" in exc.value.message.lower()


def test_import_tool_validates_the_csv_first(ctx):
    tool = GoogleSheetsImportTool()
    with pytest.raises(ToolError) as exc:
        tool.run(tool.args_model(csv_path="nope.csv"), ctx)
    assert "not found" in exc.value.message.lower()


def test_sheets_tools_declare_extra_retries_for_network_flakiness():
    assert (GoogleSheetsImportTool.max_retries or 0) >= 2
