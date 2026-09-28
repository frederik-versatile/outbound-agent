"""Small read-only dashboard: sends, replies, reply rate, and who was
emailed — for one deployment. Reads drafts_audit.json and sequences.json
through the same deployment.store abstraction as everything else (local
files or Render's Key Value store), so it works identically run locally or
deployed as a Render Web Service — see render.yaml.

Password-protected with HTTP Basic Auth (DASHBOARD_USERNAME/
DASHBOARD_PASSWORD) since it shows who's been contacted and their reply
status. Fails closed: if either env var is missing, the app refuses to
serve anything rather than falling back to no auth.

    python dashboard.py --deployment acme-corp [--port 5000]   # local

In production (Render Web Service), DEPLOYMENT_ID is set as an env var
instead of a CLI arg, since gunicorn imports this module rather than
running it as a script — see render.yaml's startCommand.
"""

from __future__ import annotations

import hmac
import json
import os
from functools import wraps
from html import escape
from typing import Any

from flask import Flask, Response, request

from deployment import Deployment, load_deployment

app = Flask(__name__)

SENT_STATUSES = {"unchanged_sent", "learned_sent"}


def _check_auth(username: str, password: str) -> bool:
    expected_user = os.environ.get("DASHBOARD_USERNAME")
    expected_pass = os.environ.get("DASHBOARD_PASSWORD")
    if not expected_user or not expected_pass:
        return False  # fail closed — see module docstring
    return hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_pass)


def requires_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        auth = request.authorization
        if not auth or not _check_auth(auth.username or "", auth.password or ""):
            return Response(
                "Authentication required.", 401, {"WWW-Authenticate": 'Basic realm="Outbound Agent Dashboard"'}
            )
        return view(*args, **kwargs)

    return wrapped


def _load_audit(deployment: Deployment) -> list[dict[str, Any]]:
    raw = deployment.store.read_text(deployment.state_key("drafts_audit.json"))
    return json.loads(raw) if raw else []


def _load_sequence_status_by_lead(deployment: Deployment) -> dict[str, str]:
    raw = deployment.store.read_text(deployment.state_key("sequences.json"))
    entries = json.loads(raw) if raw else []
    return {e["lead_key"]: e["status"] for e in entries}


def compute_dashboard_data(deployment: Deployment) -> dict[str, Any]:
    audit = _load_audit(deployment)
    status_by_lead = _load_sequence_status_by_lead(deployment)

    sent_rows = [e for e in audit if e.get("status") in SENT_STATUSES]
    sends = len(sent_rows)
    replies = sum(1 for r in sent_rows if status_by_lead.get(r["to"].lower()) == "replied")
    reply_rate = round(100 * replies / sends, 1) if sends else 0.0

    recipients = [
        {
            "stakeholder_name": r["stakeholder_name"],
            "to": r["to"],
            "account_name": r["account_name"],
            "subject": r["subject"],
            "sent_at": r.get("sent_at", ""),
            "replied": status_by_lead.get(r["to"].lower()) == "replied",
        }
        for r in sent_rows
    ]
    recipients.sort(key=lambda r: r["sent_at"], reverse=True)

    return {"sends": sends, "replies": replies, "reply_rate": reply_rate, "recipients": recipients}


def _render_html(deployment_id: str, data: dict[str, Any]) -> str:
    rows = "".join(
        f"<tr>"
        f"<td>{escape(r['stakeholder_name'])}</td>"
        f"<td>{escape(r['to'])}</td>"
        f"<td>{escape(r['account_name'])}</td>"
        f"<td>{escape(r['subject'])}</td>"
        f"<td>{escape(r['sent_at'][:10] if r['sent_at'] else '')}</td>"
        f"<td class=\"{'yes' if r['replied'] else 'no'}\">{'Replied' if r['replied'] else 'No reply yet'}</td>"
        f"</tr>"
        for r in data["recipients"]
    )
    if not rows:
        rows = '<tr><td colspan="6" class="empty">No emails sent yet.</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Outbound Agent Dashboard — {escape(deployment_id)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; padding: 2rem;
         background: #f7f7f8; color: #1a1a1a; }}
  h1 {{ font-size: 1.25rem; margin-bottom: 1.5rem; color: #444; }}
  .stats {{ display: flex; gap: 1rem; margin-bottom: 2rem; }}
  .card {{ background: white; border-radius: 8px; padding: 1.25rem 1.75rem; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
          flex: 1; }}
  .card .value {{ font-size: 2rem; font-weight: 600; }}
  .card .label {{ font-size: 0.85rem; color: #777; margin-top: 0.25rem; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden;
          box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  th, td {{ text-align: left; padding: 0.6rem 1rem; font-size: 0.9rem; border-bottom: 1px solid #eee; }}
  th {{ background: #fafafa; color: #666; font-weight: 600; }}
  td.yes {{ color: #1a7f37; font-weight: 600; }}
  td.no {{ color: #999; }}
  td.empty {{ text-align: center; color: #999; padding: 2rem; }}
</style>
</head>
<body>
<h1>Outbound Agent — {escape(deployment_id)}</h1>
<div class="stats">
  <div class="card"><div class="value">{data['sends']}</div><div class="label">Emails sent</div></div>
  <div class="card"><div class="value">{data['replies']}</div><div class="label">Replies</div></div>
  <div class="card"><div class="value">{data['reply_rate']}%</div><div class="label">Reply rate</div></div>
</div>
<table>
<thead><tr><th>Stakeholder</th><th>Email</th><th>Account</th><th>Subject</th><th>Sent</th><th>Status</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</body>
</html>"""


@app.route("/")
@requires_auth
def index() -> str:
    deployment_id = os.environ.get("DEPLOYMENT_ID")
    if not deployment_id:
        return Response("DEPLOYMENT_ID is not set.", status=500)
    deployment = load_deployment(deployment_id)
    data = compute_dashboard_data(deployment)
    return _render_html(deployment_id, data)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the dashboard locally")
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()

    os.environ["DEPLOYMENT_ID"] = args.deployment
    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
