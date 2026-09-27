"""One-time Google Sheets service-account setup for a deployment's hire
sheet cross-reference (see deployment.py's HireSheetConfig and
orchestrator.py's cross_reference_hire_sheet()).

Writes to deployment.store, not local disk — run this with REDIS_URL set to
the same Render Key Value instance the cron jobs use (see render.yaml) so
the key lands where they'll look for it. Without REDIS_URL set, it writes
to a local file instead, for local dev.

Prerequisite:
1. In Google Cloud Console, create (or reuse) a service account, enable the
   Google Sheets API for that project, and download a JSON key for it.
2. Share the target spreadsheet with that service account's email address
   (client_email in the downloaded JSON) — Viewer access is enough.
3. Set hire_sheet.enabled: true and spreadsheet_id/range in
   config/deployments/<deployment_id>.yaml.

    python setup_google_sheets.py --deployment acme-corp \\
        --key-file ~/Downloads/service-account-key.json
"""

from __future__ import annotations

import argparse
from pathlib import Path

from deployment import load_deployment
from clients.sheets_client import SheetsClient


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time Google Sheets service-account setup")
    parser.add_argument("--deployment", required=True)
    parser.add_argument(
        "--key-file", required=True,
        help="Local path to the service account JSON key downloaded from Google Cloud Console.",
    )
    args = parser.parse_args()

    deployment = load_deployment(args.deployment)
    key_path = Path(args.key_file).expanduser()
    if not key_path.exists():
        raise SystemExit(f"No file at {key_path}")

    deployment.store.write_text(deployment.secret_key("sheets_service_account.json"), key_path.read_text())

    if deployment.hire_sheet.enabled and deployment.hire_sheet.spreadsheet_id:
        client = SheetsClient(deployment)
        rows = client.read_range(deployment.hire_sheet.spreadsheet_id, deployment.hire_sheet.range)
        print(f"Read {len(rows)} row(s) from {deployment.hire_sheet.range}. Sample: {rows[:3]}")
    else:
        print(
            "Key stored. hire_sheet.enabled is false (or spreadsheet_id is empty) in "
            f"config/deployments/{args.deployment}.yaml, so no read-access check was run — "
            f"fill those in and re-run this script to verify access."
        )

    print(f"Google Sheets service account set up for deployment '{args.deployment}'.")


if __name__ == "__main__":
    main()
