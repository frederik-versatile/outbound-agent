"""Stores and validates one client's Apollo.io API key — run it yourself in a terminal.

    python setup_apollo.py --deployment acme-corp

The key is pasted at a hidden prompt, checked against a cheap organization search, and stored in
the macOS Keychain under service "refactor-outbound-<Client>", account "apollo" — one entry per
client, never a shared .env. Cloud deploys (no Keychain) can still use the env var named by
apollo_api_key_env in the client's outbound.yaml.
"""

from __future__ import annotations

import argparse
import getpass
import subprocess

from deployment import load_deployment
from clients.apollo_client import ApolloClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Store and validate a client's Apollo.io API key")
    parser.add_argument("--deployment", required=True)
    args = parser.parse_args()

    deployment = load_deployment(args.deployment)
    key = getpass.getpass(f"Paste the Apollo API key for {deployment.client} and press Enter (input hidden): ").strip()
    if not key or any(c in key for c in '"\\\n'):
        raise SystemExit("No usable key given.")

    client = ApolloClient(api_key=key, max_credits_per_run=0)  # read-only check, no enrichment credits
    try:
        client.search_organizations(keywords=["test"], per_page=1)
    except Exception as exc:
        raise SystemExit(f"Apollo rejected this key: {exc}")

    # `security -i` reads the command from stdin, so the key never appears in a process list.
    cmd = f'add-generic-password -U -s "{deployment.keychain_service}" -a "apollo" -w "{key}"\n'
    r = subprocess.run(["security", "-i"], input=cmd, text=True, capture_output=True)
    if r.returncode != 0 or "error" in (r.stdout + r.stderr).lower():
        raise SystemExit("Keychain didn't store the key.")
    print(f"✓ Apollo key valid and stored for {deployment.client} (Keychain: {deployment.keychain_service}).")


if __name__ == "__main__":
    main()
