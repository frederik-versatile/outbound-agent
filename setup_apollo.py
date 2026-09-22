"""Validates an Apollo.io API key for a deployment against a cheap
organization-search call, and reminds you which .env var to set it under.

This script does not write the key anywhere — Apollo keys live only in
.env (see .env.example), never in a per-deployment secrets file, so a
single .env stays the one place to rotate/revoke a customer's key.

    python setup_apollo.py --deployment acme-corp
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv

from deployment import ROOT, load_deployment
from clients.apollo_client import ApolloClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate an Apollo.io API key for a deployment")
    parser.add_argument("--deployment", required=True)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    deployment = load_deployment(args.deployment)

    key = os.environ.get(deployment.apollo_api_key_env)
    if not key:
        raise SystemExit(
            f"{deployment.apollo_api_key_env} is not set in .env. "
            f"Add it (see .env.example), then re-run this script."
        )

    client = ApolloClient(api_key=key, max_credits_per_run=0)  # read-only check, no enrichment credits
    try:
        client.search_organizations(keywords=["test"], per_page=1)
    except Exception as exc:
        raise SystemExit(f"Apollo API key for {deployment.apollo_api_key_env} looks invalid: {exc}")

    print(f"Apollo API key {deployment.apollo_api_key_env} is valid for deployment '{args.deployment}'.")


if __name__ == "__main__":
    main()
