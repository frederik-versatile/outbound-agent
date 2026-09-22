"""Microsoft Graph API client — DRAFT-ONLY.

This module defines create_draft / get_draft / list_drafts / get_sent_message /
update_draft / delete_draft and NOTHING ELSE. There is no send_draft, no call
to /me/sendMail or /me/messages/{id}/send, no send capability anywhere in
this file, by design. See gmail_client.py's module docstring — same rule,
same reason, checked by the same tests/test_no_send_capability.py guard.

API host is hardcoded to graph.microsoft.com and not configurable.

Token cache is read from and written back to deployment.store, not local
disk — see store.py's module docstring for why. Azure AD refresh tokens can
rotate on use (more often than Google's), so persisting the cache back
after every acquire_token_silent call matters even more here than for Gmail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import msal
import requests

if TYPE_CHECKING:
    from deployment import Deployment

GRAPH_HOST = "https://graph.microsoft.com"
GRAPH_VERSION = "v1.0"
SCOPES = ["Mail.ReadWrite"]
TRACKING_HEADER = "X-Outbound-Agent-Draft-Id"


class OutlookClient:
    def __init__(self, deployment: "Deployment"):
        self._deployment = deployment
        self.client_id = deployment.outlook_client_id
        self.tenant_id = deployment.outlook_tenant_id
        self._access_token = self._load_access_token(deployment)

    def _load_access_token(self, deployment: "Deployment") -> str:
        cache_json = deployment.store.read_text(deployment.secret_key("outlook_token.json"))
        if not cache_json:
            raise RuntimeError(
                f"No Outlook token cache stored for deployment '{deployment.deployment_id}'. "
                f"Run setup_oauth_outlook.py first."
            )
        cache = msal.SerializableTokenCache()
        cache.deserialize(cache_json)

        app = msal.PublicClientApplication(
            self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            token_cache=cache,
        )
        accounts = app.get_accounts()
        if not accounts:
            raise RuntimeError(
                f"Outlook token cache for deployment '{deployment.deployment_id}' has no account. "
                f"Run setup_oauth_outlook.py again."
            )
        result = app.acquire_token_silent(SCOPES, account=accounts[0])
        if not result or "access_token" not in result:
            raise RuntimeError("Could not silently refresh Outlook token. Run setup_oauth_outlook.py again.")

        if cache.has_state_changed:
            deployment.store.write_text(deployment.secret_key("outlook_token.json"), cache.serialize())

        return result["access_token"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}", "Content-Type": "application/json"}

    def create_draft(
        self,
        to: list[str],
        subject: str,
        body_text: str,
        tracking_id: str,
        conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /me/messages (created as a draft by default — isDraft is
        read-only and Graph sets it true for a plain create). Sets an
        internetMessageHeaders entry with the tracking id, same purpose as
        Gmail's custom header."""
        payload: dict[str, Any] = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in to],
            "internetMessageHeaders": [
                {"name": TRACKING_HEADER, "value": tracking_id},
            ],
        }
        if conversation_id:
            payload["conversationId"] = conversation_id

        resp = requests.post(
            f"{GRAPH_HOST}/{GRAPH_VERSION}/me/messages",
            headers=self._headers(),
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_draft(self, message_id: str) -> dict[str, Any] | None:
        """GET /me/messages/{id}. Returns None if the draft is gone — caller
        checks Sent Items next, same pattern as GmailClient.get_draft."""
        resp = requests.get(
            f"{GRAPH_HOST}/{GRAPH_VERSION}/me/messages/{message_id}",
            headers=self._headers(),
            timeout=30,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def list_drafts(self, filter_query: str | None = None, top: int = 25) -> dict[str, Any]:
        """GET /me/mailFolders/drafts/messages."""
        params: dict[str, Any] = {"$top": top}
        if filter_query:
            params["$filter"] = filter_query
        resp = requests.get(
            f"{GRAPH_HOST}/{GRAPH_VERSION}/me/mailFolders/drafts/messages",
            headers=self._headers(),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def get_sent_message_by_tracking_id(self, tracking_id: str) -> dict[str, Any] | None:
        """Searches Sent Items for a message carrying this tracking header,
        used once a draft has disappeared (see poll_for_edits.py)."""
        resp = requests.get(
            f"{GRAPH_HOST}/{GRAPH_VERSION}/me/mailFolders/sentitems/messages",
            headers=self._headers(),
            params={"$search": f'"{tracking_id}"'},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        messages = data.get("value", [])
        return messages[0] if messages else None

    def update_draft(self, message_id: str, **fields: Any) -> dict[str, Any]:
        """PATCH /me/messages/{id}. Not called anywhere in the normal
        pipeline — see gmail_client.update_draft's docstring for the guard
        requirement this must satisfy before it's ever wired up."""
        raise NotImplementedError(
            "update_draft is intentionally unimplemented until the baseline-hash "
            "guard described in the plan's learning-loop section is built."
        )

    def delete_draft(self, message_id: str) -> None:
        """DELETE /me/messages/{id}. Same guard requirement as update_draft."""
        raise NotImplementedError(
            "delete_draft is intentionally unimplemented until the baseline-hash "
            "guard described in the plan's learning-loop section is built."
        )
