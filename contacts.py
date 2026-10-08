"""Code-only contact store: the one place names and email addresses live.

Agents work with a person_id plus non-personal fields (title, seniority, company). They write
emails with placeholders — {{first_name}}, {{last_name}}, {{full_name}} — and this module fills in
the real values only when the draft is created in the mailbox. In the other direction,
depersonalize() turns a human-edited draft back into placeholder text before the learning stage
sees it.

Stored per deployment at state/<deployment_id>/contacts.json via deployment.store (local file or
Redis). Never exposed through any agent tool.
"""

from __future__ import annotations

import json
import re
from typing import Any

PLACEHOLDERS = ("{{first_name}}", "{{last_name}}", "{{full_name}}")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Fields an agent may see about a person. Everything else stays in the store.
SAFE_PERSON_FIELDS = ("title", "seniority", "organization_id", "organization_name", "email_status")


class PlaceholderError(ValueError):
    pass


class ContactVault:
    def __init__(self, deployment):
        self._deployment = deployment
        self._key = deployment.state_key("contacts.json")
        raw = deployment.store.read_text(self._key)
        self._people: dict[str, dict[str, Any]] = json.loads(raw) if raw else {}

    # ---- storage ---------------------------------------------------------------------------

    def _save(self) -> None:
        self._deployment.store.write_text(self._key, json.dumps(self._people, indent=2))

    def remember(self, person: dict[str, Any]) -> str | None:
        """Store an Apollo person/match record; returns its person_id."""
        pid = str(person.get("id") or person.get("person_id") or "")
        if not pid:
            return None
        entry = self._people.setdefault(pid, {})
        for key in ("first_name", "last_name", "name", "email", "email_status", "title", "seniority",
                    "organization_id"):
            if person.get(key) not in (None, ""):
                entry[key] = person[key]
        org = person.get("organization") or {}
        if isinstance(org, dict) and org.get("name"):
            entry["organization_name"] = org["name"]
        if not entry.get("name") and (entry.get("first_name") or entry.get("last_name")):
            entry["name"] = " ".join(p for p in (entry.get("first_name"), entry.get("last_name")) if p)
        self._save()
        return pid

    def get(self, person_id: str | None) -> dict[str, Any] | None:
        return self._people.get(str(person_id)) if person_id else None

    def email(self, person_id: str | None) -> str:
        return (self.get(person_id) or {}).get("email", "")

    # ---- what agents may see ---------------------------------------------------------------

    @staticmethod
    def safe_view(person: dict[str, Any]) -> dict[str, Any]:
        view = {"person_id": str(person.get("id") or person.get("person_id") or "")}
        for key in SAFE_PERSON_FIELDS:
            if person.get(key) not in (None, ""):
                view[key] = person[key]
        if "email" in person or "email_status" in person:
            view["has_email"] = bool(person.get("email"))
        return view

    # ---- placeholders ----------------------------------------------------------------------

    @staticmethod
    def check_template(*texts: str) -> None:
        """Agents must not put an email address or an unknown placeholder in a draft."""
        for text in texts:
            if _EMAIL_RE.search(text or ""):
                raise PlaceholderError("The draft contains an email address. Leave recipient details out; "
                                       "the code adds them.")
            unknown = {m for m in _PLACEHOLDER_RE.findall(text or "")} - {"first_name", "last_name", "full_name"}
            if unknown:
                raise PlaceholderError(f"Unknown placeholder(s): {', '.join(sorted(unknown))}. "
                                       f"Use only {', '.join(PLACEHOLDERS)}.")

    @staticmethod
    def merge(text: str, contact: dict[str, Any]) -> str:
        """Fill placeholders with the contact's real values (code-side, right before the mailbox)."""
        first = contact.get("first_name") or (contact.get("name") or "").split(" ")[0] or "there"
        last = contact.get("last_name") or ""
        full = contact.get("name") or " ".join(p for p in (first, last) if p)
        values = {"first_name": first, "last_name": last, "full_name": full}
        return _PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), m.group(0)), text)

    @staticmethod
    def depersonalize(text: str, contact: dict[str, Any] | None, sender_names: list[str] | None = None) -> str:
        """Turn real names/emails back into placeholders (longest first), for the learning stage."""
        if not text:
            return text
        swaps: list[tuple[str, str]] = []
        if contact:
            if contact.get("email"):
                swaps.append((contact["email"], "{{email}}"))
            if contact.get("name"):
                swaps.append((contact["name"], "{{full_name}}"))
            if contact.get("last_name"):
                swaps.append((contact["last_name"], "{{last_name}}"))
            if contact.get("first_name"):
                swaps.append((contact["first_name"], "{{first_name}}"))
        for sender in sender_names or []:
            if sender:
                swaps.append((sender, "{{sender_name}}"))
        for real, placeholder in sorted(swaps, key=lambda s: -len(s[0])):
            text = re.sub(rf"\b{re.escape(real)}\b", placeholder, text, flags=re.IGNORECASE)
        return text
