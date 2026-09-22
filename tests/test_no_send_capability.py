"""Structural safety regression guard: fails if any method or registered
tool name matching send/Send/sendMail ever appears in clients/ or tools/.

This is deliberately a dumb static text scan, not a semantic check — the
whole point (per the plan's "Safety" section) is that the no-send guarantee
must not depend on anyone reasoning carefully about a new method's behavior
before merging it. If this test ever needs an exception, that's a decision
to make explicitly and re-justify, not something to quietly work around.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parent.parent
SCANNED_DIRS = ["clients", "tools"]

# Matches a def/tool-name containing "send" as a whole word-ish fragment,
# e.g. send_draft, sendMail, send_email, mailbox_send — but not words that
# merely contain "send" as a substring incidentally (there are none expected
# in this codebase; if one legitimately appears, name it to avoid the match
# rather than loosening this pattern).
_SEND_PATTERN = re.compile(r"(?i)\bsend[a-z_]*\b")

# Lines that exist to document the absence of a send capability — allowed to
# mention the word "send" in prose/comments/docstrings/error messages.
_ALLOWLISTED_CONTEXTS = re.compile(
    r"(no send|not.*send|never send|send.*capability|send.*method|"
    r"send-capable|send_draft|messages\.send|/sendmail|sendmail call|does.*not.*send)",
    re.IGNORECASE,
)


def _iter_source_files():
    for dirname in SCANNED_DIRS:
        yield from (ROOT / dirname).glob("*.py")


def test_no_send_method_or_tool_defined():
    violations = []
    for path in _iter_source_files():
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            is_def_or_tool = stripped.startswith("def ") or stripped.startswith("async def ") or '@tool(' in stripped or '"mailbox_' in stripped
            if not is_def_or_tool:
                continue
            if _SEND_PATTERN.search(line) and not _ALLOWLISTED_CONTEXTS.search(line):
                violations.append(f"{path.relative_to(ROOT)}:{lineno}: {stripped}")

    assert not violations, (
        "Found a send-capable method or tool name in clients/ or tools/ — "
        "this codebase must never be able to send email, only draft it:\n"
        + "\n".join(violations)
    )
