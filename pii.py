"""No-PII rule: Claude never sees personal data about the people this agent contacts.

Mirrors Agents/Global/Shared/pii_guard.scrub (this repo is deployed on its own, so it can't import
the shared module). scrub() is the last line of defence on anything handed to an agent; the main
control is contacts.py, which keeps names and emails in code-only storage and gives agents a
person_id plus non-personal fields instead.
"""

from __future__ import annotations

import re

REDACTED = "[PII removed]"

PII_KEY = re.compile(
    r"^(hs_|work_|home_|personal_|contact_)?(e-?mail(_?address)?|emails|phone(_?number)?|"
    r"mobile(_?phone)?|first_?name|last_?name|full_?name|given_?name|family_?name|"
    r"street(_?address)?|address|zip(_?code)?|postal_?code|date_?of_?birth|birth_?date|"
    r"linkedin_?url|photo_?url|twitter_?url|facebook_?url|github_?url|"
    r"ssn|cpr|national_?id|passport|ip(_?address)?|to|lead_key|stakeholder_name)$", re.I)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\+\d[\d\s\-().]{6,}\d")


def scrub_text(text: str) -> str:
    return PHONE.sub(REDACTED, EMAIL.sub(REDACTED, text))


def scrub(obj):
    """Recursively replace values under personal-data keys and strip emails / phone numbers.

    A bare "name" key is NOT redacted here (organization records use it for the company name);
    people records never reach an agent unreduced — contacts.ContactVault.safe_view() does that.
    """
    if isinstance(obj, dict):
        return {k: (REDACTED if isinstance(k, str) and PII_KEY.search(k) and obj[k] not in (None, "", [], {})
                    else scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj
