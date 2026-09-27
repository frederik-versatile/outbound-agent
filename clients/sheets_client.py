"""Read-only Google Sheets client — used to cross-reference the
prioritization stage's Apollo-sourced accounts against a customer-supplied
sheet (e.g. a manually tracked "hiring a videographer" buying-signal list),
never as the primary source of accounts. See orchestrator.py's
cross_reference_hire_sheet().

Uses a service account (not the mailbox OAuth user token) — this runs
unattended on a cron schedule, and a service account avoids the interactive
consent/refresh-token dance that end-user OAuth needs. The sheet must be
shared with the service account's email address (share it like you would
with a person) before this can read it.

Read-only scope, read-only method (spreadsheets.values.get) — this client
has no write capability, matching the draft-only philosophy elsewhere in
this codebase even though sheets aren't safety-critical the way mailbox
writes are.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

if TYPE_CHECKING:
    from deployment import Deployment

SHEETS_HOST = "https://sheets.googleapis.com"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


class SheetsClient:
    def __init__(self, deployment: "Deployment"):
        key_json = deployment.store.read_text(deployment.secret_key("sheets_service_account.json"))
        if not key_json:
            raise RuntimeError(
                f"No Google Sheets service account key stored for deployment "
                f"'{deployment.deployment_id}'. Run setup_google_sheets.py first."
            )
        creds = Credentials.from_service_account_info(json.loads(key_json), scopes=SCOPES)
        self._service = build("sheets", "v4", credentials=creds)

    def read_range(self, spreadsheet_id: str, range_name: str) -> list[list[str]]:
        """Returns the raw row/column values for a range, e.g.
        [["Acme Robotics", "acmerobotics.com"], ["Northwind", "northwind.io"]].
        Empty list if the range has no data."""
        result = (
            self._service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_name)
            .execute()
        )
        return result.get("values", [])
