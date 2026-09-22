"""Subscription-status gate: both orchestrator.py and poll_for_edits.py call
require_active_subscription(deployment) as the very first thing they do,
before any Apollo or mailbox API call. If the customer's Stripe subscription
isn't active, this raises and the run stops — no cost is incurred on a
lapsed account. This is the paywall: it's enforced in code at the top of
every entrypoint, not by trusting a separately-billed invoice to match
reality.

Requires STRIPE_API_KEY in the environment and deployment.stripe_subscription_id
set in that deployment's YAML config.
"""

from __future__ import annotations

import os

import stripe

# Stripe subscription statuses that mean "keep running." Notably excludes
# past_due, unpaid, canceled, incomplete, incomplete_expired, paused.
_ACTIVE_STATUSES = {"active", "trialing"}


class SubscriptionInactive(RuntimeError):
    pass


def require_active_subscription(deployment) -> None:
    if not deployment.stripe_subscription_id:
        raise SubscriptionInactive(
            f"Deployment '{deployment.deployment_id}' has no stripe_subscription_id "
            f"configured — refusing to run rather than assume it's paid for."
        )

    api_key = os.environ.get("STRIPE_API_KEY")
    if not api_key:
        raise SubscriptionInactive("STRIPE_API_KEY is not set — refusing to run without a way to check billing.")

    stripe.api_key = api_key
    try:
        subscription = stripe.Subscription.retrieve(deployment.stripe_subscription_id)
    except stripe.error.StripeError as exc:
        raise SubscriptionInactive(f"Could not look up Stripe subscription: {exc}") from exc

    if subscription.status not in _ACTIVE_STATUSES:
        raise SubscriptionInactive(
            f"Subscription {deployment.stripe_subscription_id} for deployment "
            f"'{deployment.deployment_id}' has status '{subscription.status}', not active. Stopping."
        )
