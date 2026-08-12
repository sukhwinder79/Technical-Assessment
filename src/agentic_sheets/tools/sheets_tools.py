"""Google Sheets tools, built on the official Google Sheets API v4.

Two authentication modes:

  * ``service_account`` — headless, ideal for servers and CI. The service
    account owns the spreadsheet it creates, so the tool also uses the Drive
    API to share it with ``GOOGLE_SHARE_WITH_EMAIL`` (otherwise a human could
    never open the link).
  * ``oauth`` — desktop flow. Opens a browser once, caches a refresh token, and
    creates the sheet inside the user's own Drive.

Every failure is translated into an actionable `ToolError` — a 403 from the
Sheets API almost always means "you forgot to enable the API" or "the service
account can't see this file", and saying so is far more useful to both the
model and the user than re-raising an HttpError.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings
from ..errors import ToolError
from ..logging_setup import get_logger
from .base import Tool, ToolContext
from .data_tools import _coerce_number, read_csv

log = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

MAX_CELLS_PER_REQUEST = 40_000  # Sheets API is happy well above this; we chunk defensively.


# ---------------------------------------------------------------------------
#  Auth
# ---------------------------------------------------------------------------


def _load_credentials(settings: Settings):
    mode = settings.google_auth_mode

    if mode == "disabled":
        raise ToolError(
            "Google Sheets integration is disabled (GOOGLE_AUTH_MODE=disabled).",
            remediation=(
                "Set GOOGLE_AUTH_MODE to 'service_account' or 'oauth' in .env and provide credentials. "
                "Until then, report the Google Sheets step as skipped."
            ),
        )

    try:
        from google.oauth2 import service_account
        from google.oauth2.credentials import Credentials
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise ToolError(
            "Google API client libraries are not installed.",
            remediation="pip install -r requirements.txt",
        ) from exc

    if mode == "service_account":
        path = Path(settings.google_credentials_file).expanduser()
        if not path.exists():
            raise ToolError(
                f"Service-account key file not found: {path}",
                remediation=(
                    "Create a service account in Google Cloud, enable the Google Sheets API and the "
                    "Google Drive API, download the JSON key, and point GOOGLE_CREDENTIALS_FILE at it. "
                    "See the README section 'Google Sheets setup'."
                ),
            )
        try:
            return service_account.Credentials.from_service_account_file(str(path), scopes=SCOPES)
        except Exception as exc:  # noqa: BLE001
            raise ToolError(
                f"Service-account key file is invalid: {exc}",
                remediation="Re-download the JSON key from the Google Cloud console.",
            ) from exc

    # --- OAuth desktop flow --------------------------------------------------
    from google.auth.transport.requests import Request
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = Path(settings.google_token_file).expanduser()
    client_path = Path(settings.google_oauth_client_file).expanduser()

    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception:  # noqa: BLE001 - a corrupt token just means re-auth
            creds = None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception:  # noqa: BLE001
            creds = None

    if not client_path.exists():
        raise ToolError(
            f"OAuth client file not found: {client_path}",
            remediation=(
                "In Google Cloud, create an OAuth client of type 'Desktop app', download the JSON, "
                "and point GOOGLE_OAUTH_CLIENT_FILE at it."
            ),
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _build_services(settings: Settings):
    from googleapiclient.discovery import build

    creds = _load_credentials(settings)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    account = getattr(creds, "service_account_email", None)
    return sheets, drive, account


def _google_reason(exc: Exception) -> str:
    """Google's own error message, which is the part worth surfacing."""
    content = getattr(exc, "content", None)
    if content:
        try:
            body = json.loads(content.decode("utf-8", "replace"))
            if isinstance(body.get("error"), dict):
                error = body["error"]
                detail = error.get("message", "")
                if errors := error.get("errors"):
                    reasons = {e.get("reason") for e in errors if isinstance(e, dict)}
                    if reasons:
                        detail = f"{detail} (reason: {', '.join(sorted(filter(None, reasons)))})"
                return detail or str(exc)
        except (ValueError, AttributeError):
            pass
    return str(exc)


def _service_account_cannot_own_files(drive) -> bool:
    """Does this account have zero Drive storage?

    Google removed free Drive storage from service accounts, so a service
    account can no longer *own* a file — and creating a spreadsheet means
    creating a Drive file. The symptom is a bare 403 PERMISSION_DENIED from
    `spreadsheets.create` even though both APIs are enabled, which is
    indistinguishable from a dozen other causes unless you check the quota.
    """
    try:
        quota = drive.about().get(fields="storageQuota").execute().get("storageQuota", {})
        return str(quota.get("limit", "")) == "0"
    except Exception:  # noqa: BLE001 - diagnosis is best-effort
        return False


CANNOT_OWN_FILES_REMEDIATION = (
    "Service accounts have no Drive storage of their own, so they cannot create a "
    "spreadsheet. Two ways forward:\n"
    "  (a) Write into a sheet you own: create a blank Google Sheet, share it with "
    "{email} as Editor, and set GOOGLE_SPREADSHEET_ID to its id (the long string in "
    "its URL). This needs no extra Google setup.\n"
    "  (b) Let the agent create sheets in your own Drive: set GOOGLE_AUTH_MODE=oauth "
    "and add an OAuth desktop client (see README -> Google Sheets setup, Option B).\n"
    "A Google Workspace Shared Drive also works, if you have one."
)


def _translate_http_error(exc: Exception, action: str) -> ToolError:
    from googleapiclient.errors import HttpError

    if not isinstance(exc, HttpError):
        return ToolError(f"Google Sheets call failed while {action}: {exc}", retryable=True)

    status = getattr(getattr(exc, "resp", None), "status", None)
    reason = str(exc)

    if status == 403 and "has not been used" in reason:
        return ToolError(
            f"The Google Sheets or Drive API is not enabled for this project ({action}).",
            remediation=(
                "Open the Google Cloud console → APIs & Services → Library, then enable both "
                "'Google Sheets API' and 'Google Drive API' for the project that owns your credentials."
            ),
            details={"status": status},
        )
    if status == 403:
        return ToolError(
            f"Permission denied while {action}: {_google_reason(exc)}",
            remediation=(
                "Share the target spreadsheet with the service account's email address as Editor. "
                "If you were creating a NEW spreadsheet, note that service accounts cannot own "
                "Drive files — set GOOGLE_SPREADSHEET_ID to a sheet you own, or use "
                "GOOGLE_AUTH_MODE=oauth."
            ),
            details={"status": status},
        )
    if status == 404:
        return ToolError(
            f"Spreadsheet not found while {action}.",
            remediation="Check GOOGLE_SPREADSHEET_ID / the spreadsheet_id argument, or omit it to create a new sheet.",
            details={"status": status},
        )
    if status in (429, 500, 502, 503, 504):
        return ToolError(
            f"Google API is rate-limiting or temporarily unavailable while {action} (HTTP {status}).",
            retryable=True,
            remediation="The agent will retry with backoff.",
            details={"status": status},
        )
    return ToolError(f"Google Sheets call failed while {action}: {reason}", details={"status": status})


def _values_from_csv(path: Path) -> tuple[list[str], list[list[Any]]]:
    headers, rows = read_csv(path)
    typed = [[_coerce_number(cell) for cell in row] for row in rows]
    return headers, typed


# ==========================================================================
#  Tool: google_sheets_import
# ==========================================================================


class GoogleSheetsImportArgs(BaseModel):
    csv_path: str = Field(description="Path to the CSV file whose contents should be uploaded.")
    spreadsheet_title: str | None = Field(
        default=None,
        description="Title for a NEW spreadsheet. Ignored when spreadsheet_id is provided.",
    )
    spreadsheet_id: str | None = Field(
        default=None,
        description="Write into this existing spreadsheet instead of creating a new one.",
    )
    worksheet_name: str = Field(default="Employees", description="Tab name to write the data into.")
    share_with_email: str | None = Field(
        default=None,
        description="Give this Google account edit access. Defaults to GOOGLE_SHARE_WITH_EMAIL.",
    )
    clear_existing: bool = Field(default=True, description="Clear the worksheet before writing.")
    apply_formatting: bool = Field(default=True, description="Bold + freeze the header row and auto-size columns.")


class GoogleSheetsImportTool(Tool):
    name = "google_sheets_import"
    description = (
        "Upload the contents of a CSV file into a Google Sheet using the Google Sheets API. "
        "Creates a new spreadsheet (and shares it with the configured account) unless spreadsheet_id "
        "is supplied. Returns the spreadsheet id and a browser URL. Use this for any request to put "
        "data into Google Sheets."
    )
    args_model = GoogleSheetsImportArgs
    tags = ("google", "sheets", "write")
    max_retries = 3

    def run(self, args: GoogleSheetsImportArgs, ctx: ToolContext) -> dict[str, Any]:
        csv_path = ctx.resolve(args.csv_path)
        headers, rows = _values_from_csv(csv_path)
        values = [headers, *rows]

        settings = ctx.settings
        sheets, drive, service_account_email = _build_services(settings)

        spreadsheet_id = args.spreadsheet_id or settings.google_spreadsheet_id
        created = False
        title = args.spreadsheet_title or settings.google_default_spreadsheet_title

        ctx.events.emit(
            "tool_started",
            "Connecting to the Google Sheets API…",
            tool=self.name,
            phase="google_auth",
        )

        try:
            if not spreadsheet_id:
                body = {
                    "properties": {"title": title},
                    "sheets": [{"properties": {"title": args.worksheet_name[:100]}}],
                }
                try:
                    created_sheet = (
                        sheets.spreadsheets()
                        .create(body=body, fields="spreadsheetId,spreadsheetUrl")
                        .execute()
                    )
                except Exception as exc:  # noqa: BLE001
                    # A bare 403 here is almost always the service-account storage
                    # limitation, which is worth naming precisely — the generic
                    # "permission denied" sends people hunting the wrong problem.
                    status = getattr(getattr(exc, "resp", None), "status", None)
                    if status == 403 and _service_account_cannot_own_files(drive):
                        raise ToolError(
                            "This service account cannot create a Google Sheet: it has no Drive "
                            "storage quota, and creating a spreadsheet means creating a Drive file "
                            "that someone must own.",
                            remediation=CANNOT_OWN_FILES_REMEDIATION.format(
                                email=service_account_email or "the service account"
                            ),
                            details={"status": status, "google_reason": _google_reason(exc)},
                        ) from exc
                    raise
                spreadsheet_id = created_sheet["spreadsheetId"]
                created = True
                log.info("sheets.created", spreadsheet_id=spreadsheet_id, title=title)

            meta = sheets.spreadsheets().get(spreadsheetId=spreadsheet_id, fields="spreadsheetUrl,sheets.properties").execute()
            spreadsheet_url = meta.get("spreadsheetUrl", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}")
            existing = {s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])}

            worksheet = args.worksheet_name[:100]
            if worksheet not in existing:
                response = (
                    sheets.spreadsheets()
                    .batchUpdate(
                        spreadsheetId=spreadsheet_id,
                        body={"requests": [{"addSheet": {"properties": {"title": worksheet}}}]},
                    )
                    .execute()
                )
                sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]
            else:
                sheet_id = existing[worksheet]["sheetId"]

            if args.clear_existing:
                sheets.spreadsheets().values().clear(
                    spreadsheetId=spreadsheet_id, range=f"'{worksheet}'"
                ).execute()

            ctx.events.emit(
                "tool_started",
                f"Uploading {len(rows)} rows to Google Sheets…",
                tool=self.name,
                phase="google_upload",
            )

            update = (
                sheets.spreadsheets()
                .values()
                .update(
                    spreadsheetId=spreadsheet_id,
                    range=f"'{worksheet}'!A1",
                    valueInputOption="USER_ENTERED",
                    body={"values": values},
                )
                .execute()
            )

            if args.apply_formatting:
                self._format(sheets, spreadsheet_id, sheet_id, len(headers))

        except ToolError:
            raise  # already diagnosed precisely above
        except Exception as exc:  # noqa: BLE001
            raise _translate_http_error(exc, "writing to Google Sheets") from exc

        shared_with = None
        share_target = args.share_with_email or settings.google_share_with_email
        # Applied on reused spreadsheets too, not just freshly created ones: when
        # GOOGLE_SPREADSHEET_ID points at a service-account-owned sheet, skipping
        # this leaves a link nobody but the credentials can open. Granting an
        # existing permission is a no-op on Drive's side.
        if share_target:
            shared_with = self._share(drive, spreadsheet_id, share_target)

        ctx.memory.remember("last_spreadsheet_id", spreadsheet_id)
        ctx.memory.remember("last_spreadsheet_url", spreadsheet_url)

        result = {
            "ok": True,
            "spreadsheet_id": spreadsheet_id,
            "spreadsheet_url": spreadsheet_url,
            "worksheet_name": worksheet,
            "created_new_spreadsheet": created,
            "auth_mode": settings.google_auth_mode,
            "service_account_email": service_account_email,
            "rows_written": len(values),
            "data_rows": len(rows),
            "columns_written": len(headers),
            "updated_cells": update.get("updatedCells"),
            "updated_range": update.get("updatedRange"),
            "shared_with": shared_with,
        }
        if share_target and not shared_with:
            result["warning"] = (
                f"The data was written but the spreadsheet could not be shared with "
                f"{share_target}. Only accounts that already have access can open it."
            )
        log.info("sheets.import.done", **{k: v for k, v in result.items() if k != "ok"})
        return result

    # ---- helpers -----------------------------------------------------------

    @staticmethod
    def _format(sheets, spreadsheet_id: str, sheet_id: int, column_count: int) -> None:
        requests = [
            {
                "repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": {"red": 0.18, "green": 0.33, "blue": 0.59},
                            "horizontalAlignment": "CENTER",
                            "textFormat": {
                                "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                                "bold": True,
                            },
                        }
                    },
                    "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)",
                }
            },
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                    "fields": "gridProperties.frozenRowCount",
                }
            },
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": max(column_count, 1),
                    }
                }
            },
            {
                "setBasicFilter": {
                    "filter": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "startColumnIndex": 0}}
                }
            },
        ]
        try:
            sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            ).execute()
        except Exception as exc:  # noqa: BLE001 - cosmetic only, never fail the import
            log.warning("sheets.format.failed", error=str(exc))

    @staticmethod
    def _share(drive, spreadsheet_id: str, email: str) -> str | None:
        try:
            drive.permissions().create(
                fileId=spreadsheet_id,
                body={"type": "user", "role": "writer", "emailAddress": email},
                sendNotificationEmail=False,
                fields="id",
            ).execute()
            log.info("sheets.shared", spreadsheet_id=spreadsheet_id, email=email)
            return email
        except Exception as exc:  # noqa: BLE001
            log.warning("sheets.share.failed", email=email, error=str(exc))
            return None


# ==========================================================================
#  Tool: google_sheets_verify
# ==========================================================================


class GoogleSheetsVerifyArgs(BaseModel):
    spreadsheet_id: str = Field(description="The spreadsheet to read back.")
    worksheet_name: str = Field(default="Employees", description="Tab to verify.")
    expected_row_count: int | None = Field(
        default=None, description="Expected number of DATA rows (excluding the header)."
    )
    expected_columns: list[str] | None = Field(default=None, description="Expected header names, in order.")


class GoogleSheetsVerifyTool(Tool):
    name = "google_sheets_verify"
    description = (
        "Read a Google Sheet back through the API and confirm the upload landed: header row, "
        "row count and a sample of the data. Call this after google_sheets_import so the final "
        "report states verified success rather than assumed success."
    )
    args_model = GoogleSheetsVerifyArgs
    tags = ("google", "sheets", "verify")
    max_retries = 3

    def run(self, args: GoogleSheetsVerifyArgs, ctx: ToolContext) -> dict[str, Any]:
        sheets, _drive, _account = _build_services(ctx.settings)

        try:
            response = (
                sheets.spreadsheets()
                .values()
                .get(spreadsheetId=args.spreadsheet_id, range=f"'{args.worksheet_name}'")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_http_error(exc, "reading the spreadsheet back") from exc

        values = [row for row in response.get("values", []) if any(str(c).strip() for c in row)]
        if not values:
            raise ToolError(
                f"Worksheet '{args.worksheet_name}' is empty.",
                remediation="Run google_sheets_import before verifying.",
            )

        headers = [str(cell) for cell in values[0]]
        data_rows = len(values) - 1

        checks: dict[str, Any] = {}
        problems: list[str] = []

        if args.expected_row_count is not None:
            passed = data_rows == args.expected_row_count
            checks["row_count"] = {"expected": args.expected_row_count, "actual": data_rows, "passed": passed}
            if not passed:
                problems.append(f"Expected {args.expected_row_count} data rows but found {data_rows}.")

        if args.expected_columns is not None:
            passed = [h.strip() for h in headers] == [c.strip() for c in args.expected_columns]
            checks["columns"] = {"expected": args.expected_columns, "actual": headers, "passed": passed}
            if not passed:
                problems.append("Header row does not match the expected columns.")

        verified = not problems
        log.info("sheets.verify", spreadsheet_id=args.spreadsheet_id, rows=data_rows, verified=verified)

        return {
            "ok": True,
            "verified": verified,
            "spreadsheet_id": args.spreadsheet_id,
            "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{args.spreadsheet_id}",
            "worksheet_name": args.worksheet_name,
            "columns": headers,
            "data_row_count": data_rows,
            "checks": checks,
            "problems": problems,
            "sample_rows": values[1:4],
        }
