"""One-time Gmail OAuth setup for a deployment. Mirrors the Google Ads
agent's setup_oauth.py pattern: opens a local browser flow, the human signs
in themselves (this script never touches their password), and the resulting
token is persisted for GmailClient to use.

Writes to deployment.store, not local disk — run this with REDIS_URL set to
the same Render Key Value instance the cron jobs use (see render.yaml) so
the token lands where they'll look for it. Without REDIS_URL set, it writes
to local files instead, for local dev.

Prerequisite: download an OAuth client ID (Desktop app type) from Google
Cloud Console for a project with the Gmail API enabled.

    python setup_oauth_gmail.py --deployment acme-corp \\
        --client-secret-file ~/Downloads/client_secret_1234.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from deployment import load_deployment
from clients.gmail_client import SCOPES


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time Gmail OAuth setup")
    parser.add_argument("--deployment", required=True)
    parser.add_argument(
        "--client-secret-file", required=True,
        help="Local path to the OAuth client ID JSON downloaded from Google Cloud Console.",
    )
    args = parser.parse_args()

    deployment = load_deployment(args.deployment)
    client_secret_path = Path(args.client_secret_file).expanduser()
    if not client_secret_path.exists():
        raise SystemExit(f"No file at {client_secret_path}")

    client_config = json.loads(client_secret_path.read_text())
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    deployment.store.write_text(deployment.secret_key("gmail_client_secret.json"), client_secret_path.read_text())
    deployment.store.write_text(deployment.secret_key("gmail_token.json"), creds.to_json())

    print(f"Gmail OAuth complete for deployment '{args.deployment}'. Token stored.")


if __name__ == "__main__":
    main()
