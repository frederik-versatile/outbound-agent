"""One-time Outlook (Microsoft Graph) OAuth setup for a deployment, using
MSAL's device code flow — the human visits a short URL and enters a code,
this script never touches their password. Resulting token cache is
persisted for OutlookClient to refresh silently on later runs.

Writes to deployment.store, not local disk — run this with REDIS_URL set to
the same Render Key Value instance the cron jobs use (see render.yaml) so
the token lands where they'll look for it. Without REDIS_URL set, it writes
to a local file instead, for local dev.

Prerequisite: register an app in Azure AD / Entra ID as a public client with
the Mail.ReadWrite delegated permission, and put its client_id/tenant_id in
config/deployments/<deployment_id>.yaml under `outlook:` before running this.

    python setup_oauth_outlook.py --deployment acme-corp
"""

from __future__ import annotations

import argparse

import msal

from deployment import load_deployment
from clients.outlook_client import SCOPES


def main() -> None:
    parser = argparse.ArgumentParser(description="One-time Outlook OAuth setup")
    parser.add_argument("--deployment", required=True)
    args = parser.parse_args()

    deployment = load_deployment(args.deployment)
    if not deployment.outlook_client_id or not deployment.outlook_tenant_id:
        raise SystemExit(
            f"config/deployments/{args.deployment}.yaml is missing outlook.client_id / "
            f"outlook.tenant_id. Register an app in Azure AD first and fill those in."
        )

    token_key = deployment.secret_key("outlook_token.json")
    cache = msal.SerializableTokenCache()
    existing = deployment.store.read_text(token_key)
    if existing:
        cache.deserialize(existing)

    app = msal.PublicClientApplication(
        deployment.outlook_client_id,
        authority=f"https://login.microsoftonline.com/{deployment.outlook_tenant_id}",
        token_cache=cache,
    )

    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise SystemExit(f"Failed to start device flow: {flow}")
    print(flow["message"])

    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise SystemExit(f"Outlook OAuth failed: {result.get('error_description', result)}")

    deployment.store.write_text(token_key, cache.serialize())
    print(f"Outlook OAuth complete for deployment '{args.deployment}'. Token stored.")


if __name__ == "__main__":
    main()
