from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import orchestrator
from deployment import HireSheetConfig
from orchestrator import _normalize_domain, cross_reference_hire_sheet


def test_normalize_domain_strips_protocol_and_www():
    assert _normalize_domain("https://www.Acme.com/") == "acme.com"
    assert _normalize_domain("http://acme.com") == "acme.com"
    assert _normalize_domain("acme.com/careers") == "acme.com"
    assert _normalize_domain("ACME.COM") == "acme.com"


@dataclass
class _FakeDeployment:
    hire_sheet: HireSheetConfig


def _accounts():
    return [
        {"org_id": "1", "name": "Acme Robotics", "domain": "acmerobotics.com", "score": 60, "rationale": "Matches ICP industry"},
        {"org_id": "2", "name": "Northwind", "domain": "northwind.io", "score": 80, "rationale": "Recently funded"},
    ]


def _log():
    calls = []
    return calls, (lambda stage, event, detail="": calls.append((stage, event, detail)))


def test_disabled_hire_sheet_is_a_noop():
    d = _FakeDeployment(hire_sheet=HireSheetConfig(enabled=False))
    accounts = _accounts()
    calls, log = _log()
    result = cross_reference_hire_sheet(d, accounts, log)
    assert result == accounts
    assert calls == []


def test_missing_spreadsheet_id_is_a_noop():
    d = _FakeDeployment(hire_sheet=HireSheetConfig(enabled=True, spreadsheet_id=""))
    accounts = _accounts()
    calls, log = _log()
    result = cross_reference_hire_sheet(d, accounts, log)
    assert result == accounts


def test_matching_domain_boosts_score_and_appends_rationale():
    d = _FakeDeployment(hire_sheet=HireSheetConfig(
        enabled=True, spreadsheet_id="sheet-1", domain_column=1, score_boost=15
    ))
    fake_client = MagicMock()
    fake_client.read_range.return_value = [["Acme Robotics", "https://www.acmerobotics.com/careers"]]

    calls, log = _log()
    with patch.object(orchestrator, "SheetsClient", return_value=fake_client):
        result = cross_reference_hire_sheet(d, _accounts(), log)

    acme = next(a for a in result if a["org_id"] == "1")
    northwind = next(a for a in result if a["org_id"] == "2")
    assert acme["score"] == 75  # 60 + 15
    assert "hire-signal sheet" in acme["rationale"]
    assert northwind["score"] == 80  # unmatched, unchanged
    assert ("prioritization", "hire_sheet_matches", "1") in calls


def test_score_boost_caps_at_100():
    d = _FakeDeployment(hire_sheet=HireSheetConfig(enabled=True, spreadsheet_id="sheet-1", score_boost=50))
    fake_client = MagicMock()
    fake_client.read_range.return_value = [["Acme Robotics", "acmerobotics.com"]]

    accounts = [{"org_id": "1", "name": "Acme Robotics", "domain": "acmerobotics.com", "score": 90, "rationale": "x"}]
    _, log = _log()
    with patch.object(orchestrator, "SheetsClient", return_value=fake_client):
        result = cross_reference_hire_sheet(d, accounts, log)

    assert result[0]["score"] == 100


def test_sheet_read_failure_leaves_accounts_unmodified():
    d = _FakeDeployment(hire_sheet=HireSheetConfig(enabled=True, spreadsheet_id="sheet-1"))
    accounts = _accounts()
    calls, log = _log()

    with patch.object(orchestrator, "SheetsClient", side_effect=RuntimeError("no service account key stored")):
        result = cross_reference_hire_sheet(d, accounts, log)

    assert result == accounts  # unmodified, run continues
    assert any(event == "hire_sheet_read_failed" for _, event, _ in calls)
