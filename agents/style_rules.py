"""Global, non-negotiable copy rules — apply to every stage that writes
email copy (drafting_agent, sequence_agent), regardless of deployment or
learned style_notes.md. Unlike style_notes.md (per-customer, learned,
mutable), these are fixed: no per-customer preference or human edit should
ever be interpreted as overriding them. See learning_agent.py's prompt for
the explicit instruction not to write anything into style_notes.md that
would contradict these.

The em-dash rule is also enforced in code, not just here — see
tools/mailbox_tools.py's EM_DASH check on mailbox_create_draft — so a
missed instruction can't quietly slip through as a live draft.
"""

from __future__ import annotations

EM_DASH = "—"

GLOBAL_STYLE_RULES = f"""GLOBAL RULES (apply to every email you write, no exceptions, never overridden by a
learned preference or a customer's positioning doc):
- Never use an em-dash ({EM_DASH}). If you're tempted to reach for one, the sentence
  usually just wants to be two sentences, or a comma. This is enforced in code too:
  mailbox_create_draft will refuse a draft containing one.
- Never use the "it's not X, it's Y" contrastive sentence structure (e.g. "This isn't
  about volume, it's about precision" or "We're not another tool, we're a partner").
  It reads as generic AI-written copy. State the point directly instead."""
