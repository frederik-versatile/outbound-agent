"""Gmail API client — DRAFT-ONLY.

This module defines create_draft / get_draft / list_drafts / get_message /
update_draft / delete_draft and NOTHING ELSE. There is no send_draft, no call
to users.messages.send, no send capability anywhere in this file, by design —
outbound emails are never sent by this codebase, only drafted for a human to
review and send themselves.

tests/test_no_send_capability.py statically scans this file (and the rest of
clients/ and tools/) and fails CI if any method or tool name matching
send/Send/sendMail ever appears — see README "Safety" for why this is
enforced in code rather than only in a system prompt.

API host is hardcoded to googleapis.com and not configurable.

Tokens are read from and written back to deployment.store, not local disk —
see store.py's module docstring for why: a refreshed access token (or, less
commonly, a rotated refresh token) has to survive to the NEXT cron
invocation, which may run in a completely fresh container.
"""

from __future__ import annotations

import base64
import json
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Any

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

if TYPE_CHECKING:
    from deployment import Deployment

GMAIL_HOST = "https://www.googleapis.com"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
TRACKING_HEADER = "X-Outbound-Agent-Draft-Id"


class GmailClient:
    def __init__(self, deployment: "Deployment"):
        self._deployment = deployment
        self._creds = self._load_credentials(deployment)
        self._service = build("gmail", "v1", credentials=self._creds)

    @staticmethod
    def _load_credentials(deployment: "Deployment") -> Credentials:
        token_json = deployment.store.read_text(deployment.secret_key("gmail_token.json"))
        if not token_json:
            raise RuntimeError(
                f"No Gmail token stored for deployment '{deployment.deployment_id}'. "
                f"Run setup_oauth_gmail.py first."
            )
        creds = Credentials.from_authorized_user_info(json.loads(token_json), SCOPES)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
                deployment.store.write_text(deployment.secret_key("gmail_token.json"), creds.to_json())
            else:
                raise RuntimeError(
                    f"Gmail token for deployment '{deployment.deployment_id}' is invalid "
                    f"and not refreshable. Run setup_oauth_gmail.py again."
                )
        return creds

    def create_draft(
        self,
        to: str,
        subject: str,
        body_text: str,
        tracking_id: str,
        thread_id: str | None = None,
        in_reply_to: str | None = None,
    ) -> dict[str, Any]:
        """users.drafts.create. Embeds the tracking header so poll_for_edits.py
        can find this draft (or its sent version) again later."""
        message = MIMEText(body_text)
        message["to"] = to
        message["subject"] = subject
        message[TRACKING_HEADER] = tracking_id
        if in_reply_to:
            message["In-Reply-To"] = in_reply_to
            message["References"] = in_reply_to

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        body: dict[str, Any] = {"message": {"raw": raw}}
        if thread_id:
            body["message"]["threadId"] = thread_id

        return self._service.users().drafts().create(userId="me", body=body).execute()

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        """users.drafts.get. Returns None (not an exception) if the draft is
        gone — the caller (mailbox_client / poll_for_edits) treats that as
        "possibly sent" and checks Sent next."""
        try:
            return self._service.users().drafts().get(userId="me", id=draft_id, format="full").execute()
        except Exception as exc:  # googleapiclient raises HttpError with .resp.status
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return None
            raise

    def list_drafts(self, label_id: str | None = None, page_token: str | None = None) -> dict[str, Any]:
        """users.drafts.list, optionally scoped to a tracking label."""
        params: dict[str, Any] = {"userId": "me"}
        if label_id:
            params["q"] = f"label:{label_id}"
        if page_token:
            params["pageToken"] = page_token
        return self._service.users().drafts().list(**params).execute()

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        """users.messages.get — used to read the as-sent version of a draft
        that has disappeared, by searching Sent for its tracking id."""
        try:
            return self._service.users().messages().get(userId="me", id=message_id, format="full").execute()
        except Exception as exc:
            if getattr(getattr(exc, "resp", None), "status", None) == 404:
                return None
            raise

    def find_sent_by_tracking_id(self, tracking_id: str) -> dict[str, Any] | None:
        """Searches Sent Mail for a message carrying this tracking header."""
        resp = (
            self._service.users()
            .messages()
            .list(userId="me", q=f'in:sent "{tracking_id}"')
            .execute()
        )
        messages = resp.get("messages", [])
        if not messages:
            return None
        return self.get_message(messages[0]["id"])

    def update_draft(self, draft_id: str, **fields: Any) -> dict[str, Any]:
        """users.drafts.update. Not called anywhere in the normal pipeline —
        see README "Safety": operator tooling that uses this must check the
        draft's stored baseline hash first and refuse if it has diverged, to
        avoid clobbering a human's in-progress edit."""
        raise NotImplementedError(
            "update_draft is intentionally unimplemented until the baseline-hash "
            "guard described in the plan's learning-loop section is built. "
            "No agent in the normal pipeline should ever need this method."
        )

    def delete_draft(self, draft_id: str) -> None:
        """users.drafts.delete. Same guard requirement as update_draft."""
        raise NotImplementedError(
            "delete_draft is intentionally unimplemented until the baseline-hash "
            "guard described in the plan's learning-loop section is built."
        )
