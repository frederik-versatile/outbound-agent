import base64
from unittest.mock import MagicMock, patch

from clients.gmail_client import GmailClient, TRACKING_HEADER as GMAIL_TRACKING_HEADER
from clients.outlook_client import OutlookClient, TRACKING_HEADER as OUTLOOK_TRACKING_HEADER
from clients.mailbox_client import normalize_for_diff


# --- normalize_for_diff: pure-function tests, no credentials needed ---

def test_normalize_strips_html():
    assert normalize_for_diff("<p>Hello <b>there</b></p>") == "Hello there"


def test_normalize_preserves_paragraph_breaks_but_not_inline_formatting():
    html = "<div>First paragraph, <b>bolded</b> word inline.</div><div>Second paragraph.</div>"
    assert normalize_for_diff(html) == "First paragraph, bolded word inline.\nSecond paragraph."


def test_normalize_trims_quoted_reply():
    text = "My reply here.\n\nOn Mon, Jan 1, 2026 at 1:00 PM Jordan Reyes wrote:\n> original text"
    assert normalize_for_diff(text) == "My reply here."


def test_normalize_trims_signature():
    text = "Thanks,\nJordan\n--\nJordan Reyes | VP Sales"
    assert normalize_for_diff(text) == "Thanks,\nJordan"


def test_normalize_leaves_plain_text_unchanged():
    assert normalize_for_diff("Just plain text, nothing special.") == "Just plain text, nothing special."


# --- GmailClient: mock the googleapiclient service, no real OAuth ---

class _FakeHttpError(Exception):
    def __init__(self, status):
        self.resp = MagicMock(status=status)


def _fake_gmail_client() -> GmailClient:
    client = GmailClient.__new__(GmailClient)
    client._service = MagicMock()
    return client


def test_gmail_create_draft_embeds_tracking_header():
    client = _fake_gmail_client()
    captured = {}

    def fake_create(userId, body):
        captured["body"] = body
        call = MagicMock()
        call.execute.return_value = {"id": "draft-123"}
        return call

    client._service.users.return_value.drafts.return_value.create.side_effect = fake_create

    result = client.create_draft(to="a@example.com", subject="Hi", body_text="Hello there", tracking_id="track-1")

    assert result["id"] == "draft-123"
    raw = captured["body"]["message"]["raw"]
    decoded = base64.urlsafe_b64decode(raw + "===").decode()
    assert GMAIL_TRACKING_HEADER in decoded
    assert "track-1" in decoded
    assert "Hello there" in decoded


def test_gmail_get_draft_returns_none_on_404():
    client = _fake_gmail_client()
    call = MagicMock()
    call.execute.side_effect = _FakeHttpError(404)
    client._service.users.return_value.drafts.return_value.get.return_value = call

    assert client.get_draft("missing-draft") is None


def test_gmail_get_draft_reraises_non_404():
    client = _fake_gmail_client()
    call = MagicMock()
    call.execute.side_effect = _FakeHttpError(500)
    client._service.users.return_value.drafts.return_value.get.return_value = call

    try:
        client.get_draft("some-draft")
        assert False, "expected exception to propagate"
    except _FakeHttpError:
        pass


# --- OutlookClient: mock requests, no real MSAL token ---

def _fake_outlook_client() -> OutlookClient:
    client = OutlookClient.__new__(OutlookClient)
    client._access_token = "fake-token"
    client.client_id = "fake-client-id"
    client.tenant_id = "fake-tenant-id"
    return client


@patch("clients.outlook_client.requests.post")
def test_outlook_create_draft_embeds_tracking_header(mock_post):
    mock_response = MagicMock()
    mock_response.json.return_value = {"id": "msg-123"}
    mock_response.raise_for_status.return_value = None
    mock_post.return_value = mock_response

    client = _fake_outlook_client()
    result = client.create_draft(to=["a@example.com"], subject="Hi", body_text="Hello there", tracking_id="track-1")

    assert result["id"] == "msg-123"
    payload = mock_post.call_args.kwargs["json"]
    headers = {h["name"]: h["value"] for h in payload["internetMessageHeaders"]}
    assert headers[OUTLOOK_TRACKING_HEADER] == "track-1"
    assert payload["body"]["content"] == "Hello there"


@patch("clients.outlook_client.requests.get")
def test_outlook_get_draft_returns_none_on_404(mock_get):
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_get.return_value = mock_response

    client = _fake_outlook_client()
    assert client.get_draft("missing-message") is None
