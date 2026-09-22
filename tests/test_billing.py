from dataclasses import dataclass
from unittest.mock import patch

import pytest
import stripe

from billing import SubscriptionInactive, require_active_subscription


@dataclass
class _FakeDeployment:
    deployment_id: str = "acme-corp"
    stripe_subscription_id: str = "sub_123"


def _fake_subscription(status: str):
    class _Sub:
        pass

    sub = _Sub()
    sub.status = status
    return sub


def test_refuses_when_no_subscription_id_configured():
    with pytest.raises(SubscriptionInactive, match="no stripe_subscription_id"):
        require_active_subscription(_FakeDeployment(stripe_subscription_id=""))


def test_refuses_when_stripe_api_key_missing(monkeypatch):
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    with pytest.raises(SubscriptionInactive, match="STRIPE_API_KEY"):
        require_active_subscription(_FakeDeployment())


def test_passes_for_active_subscription(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_fake")
    with patch.object(stripe.Subscription, "retrieve", return_value=_fake_subscription("active")):
        require_active_subscription(_FakeDeployment())  # should not raise


def test_passes_for_trialing_subscription(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_fake")
    with patch.object(stripe.Subscription, "retrieve", return_value=_fake_subscription("trialing")):
        require_active_subscription(_FakeDeployment())  # should not raise


@pytest.mark.parametrize("status", ["past_due", "canceled", "unpaid", "incomplete", "incomplete_expired", "paused"])
def test_refuses_for_inactive_statuses(monkeypatch, status):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_fake")
    with patch.object(stripe.Subscription, "retrieve", return_value=_fake_subscription(status)):
        with pytest.raises(SubscriptionInactive, match=status):
            require_active_subscription(_FakeDeployment())


def test_refuses_when_stripe_lookup_errors(monkeypatch):
    monkeypatch.setenv("STRIPE_API_KEY", "sk_test_fake")
    with patch.object(stripe.Subscription, "retrieve", side_effect=stripe.error.StripeError("boom")):
        with pytest.raises(SubscriptionInactive, match="Could not look up"):
            require_active_subscription(_FakeDeployment())
