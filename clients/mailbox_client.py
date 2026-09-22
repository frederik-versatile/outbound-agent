"""Provider-agnostic mailbox facade — picks GmailClient or OutlookClient from
deployment config so the rest of the codebase (tools, agents, poll_for_edits)
never branches on mail_provider itself.

Draft-only by construction: this facade only exposes methods that exist on
both underlying clients, and neither underlying client has a send method to
expose in the first place.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from bs4 import BeautifulSoup

from clients.gmail_client import GmailClient
from clients.outlook_client import OutlookClient
from deployment import Deployment

# Heuristics for trimming quoted replies / signature blocks before diffing,
# so a human's edit isn't drowned out by boilerplate. Deliberately simple and
# over-inclusive is fine here — the learning loop treats the diff as a signal
# to interpret, not ground truth to trust blindly.
_QUOTE_MARKERS = [
    re.compile(r"^On .+ wrote:$", re.MULTILINE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^From:.+Sent:.+To:.+Subject:", re.MULTILINE | re.DOTALL),
]
_SIGNATURE_MARKER = re.compile(r"^--\s*$", re.MULTILINE)


def _strip_html(html_or_text: str) -> str:
    if "<" not in html_or_text:
        return html_or_text
    soup = BeautifulSoup(html_or_text, "html.parser")
    # Only block-level boundaries become newlines — inline tags (b, i, span,
    # a) must not split a sentence, or a bolded word would come out on its
    # own line and swamp the diff with formatting noise.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "li", "tr"]):
        block.append("\n")
    text = soup.get_text()
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _trim_quoted_and_signature(text: str) -> str:
    for pattern in _QUOTE_MARKERS:
        match = pattern.search(text)
        if match:
            text = text[: match.start()]
    sig_match = _SIGNATURE_MARKER.search(text)
    if sig_match:
        text = text[: sig_match.start()]
    return text.strip()


def normalize_for_diff(body: str) -> str:
    """HTML-strip + quote/signature-trim, so before_text and after_text are
    comparable apples-to-apples regardless of provider or client formatting."""
    return _trim_quoted_and_signature(_strip_html(body))


class MailboxClient:
    def __init__(self, deployment: Deployment, provider_override: Literal["gmail", "outlook"] | None = None):
        provider = provider_override or deployment.mail_provider
        self.provider = provider

        if provider == "gmail":
            self._impl = GmailClient(deployment)
        elif provider == "outlook":
            self._impl = OutlookClient(deployment)
        else:
            raise ValueError(f"Unknown mail_provider: {provider!r}")

    def create_draft(self, to: str, subject: str, body_text: str, tracking_id: str) -> dict[str, Any]:
        if self.provider == "gmail":
            result = self._impl.create_draft(to=to, subject=subject, body_text=body_text, tracking_id=tracking_id)
            return {"draft_ref": result["id"], "provider": "gmail", "raw": result}
        else:
            result = self._impl.create_draft(to=[to], subject=subject, body_text=body_text, tracking_id=tracking_id)
            return {"draft_ref": result["id"], "provider": "outlook", "raw": result}

    def get_draft(self, draft_ref: str) -> dict[str, Any] | None:
        return self._impl.get_draft(draft_ref)

    def get_finalized_text(self, tracking_id: str, draft_ref: str) -> str | None:
        """Returns the as-sent body if the draft was sent, else None.

        Checks the draft still exists first (cheap); if it's gone, searches
        Sent for the tracking id — the provider-specific "disappeared from
        Drafts -> check Sent" logic lives here so poll_for_edits.py stays
        provider-agnostic.
        """
        still_draft = self._impl.get_draft(draft_ref)
        if still_draft is not None:
            return None

        if self.provider == "gmail":
            sent = self._impl.find_sent_by_tracking_id(tracking_id)
            if not sent:
                return None
            body = _extract_gmail_body(sent)
        else:
            sent = self._impl.get_sent_message_by_tracking_id(tracking_id)
            if not sent:
                return None
            body = sent.get("body", {}).get("content", "")

        return normalize_for_diff(body)

    def get_current_draft_text(self, draft_ref: str) -> str | None:
        """Returns the current body of a still-open draft (for the optional
        stable-but-unsent learning signal), normalized for diffing."""
        draft = self._impl.get_draft(draft_ref)
        if draft is None:
            return None
        if self.provider == "gmail":
            body = _extract_gmail_body(draft)
        else:
            body = draft.get("body", {}).get("content", "")
        return normalize_for_diff(body)


def _extract_gmail_body(message: dict[str, Any]) -> str:
    """Gmail's API returns a nested MIME part tree; find the first text part."""
    import base64

    def walk(part: dict[str, Any]) -> str | None:
        body = part.get("body", {})
        if body.get("data"):
            decoded = base64.urlsafe_b64decode(body["data"] + "===")
            return decoded.decode("utf-8", errors="replace")
        for sub in part.get("parts", []) or []:
            result = walk(sub)
            if result:
                return result
        return None

    return walk(message.get("payload", {})) or ""
