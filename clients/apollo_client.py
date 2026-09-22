"""Thin wrapper around the Apollo.io API: organization search, people search,
and email enrichment/verification.

Endpoint paths/params verified against Apollo's public API reference
(docs.apollo.io/reference/*) at implementation time — re-check against the
live reference if Apollo changes their API, since this client hardcodes them.

Host is hardcoded and not overridable by any tool input or config value — see
README "Safety" section. Enrichment calls are metered (Apollo charges credits
per resolved contact), so this client tracks spend per instance and refuses
once max_credits_per_run is exceeded, independent of whatever the calling
agent's prompt says.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import requests

APOLLO_HOST = "https://api.apollo.io"

# Apollo ids are long hex strings. Collapsed to a single "id" placeholder in
# fixture mode so one fixture file covers any id, rather than needing a file
# per literal id. Threshold is deliberately hex-only and long (20+) so it
# never collides with a real path word.
_ID_SEGMENT = re.compile(r"/[0-9a-fA-F]{20,}(?=/|$)")


class ApolloCreditLimitExceeded(RuntimeError):
    pass


class ApolloClient:
    def __init__(
        self,
        api_key: str,
        max_credits_per_run: int | None = None,
        mode: Literal["live", "fixture"] = "live",
        fixtures_dir: Path | None = None,
    ):
        self.api_key = api_key
        self.max_credits_per_run = max_credits_per_run
        self.mode = mode
        self.fixtures_dir = fixtures_dir
        self._credits_used = 0

    def credits_used_this_run(self) -> int:
        return self._credits_used

    def _charge(self, credits: int) -> None:
        if self.max_credits_per_run is not None and (
            self._credits_used + credits > self.max_credits_per_run
        ):
            raise ApolloCreditLimitExceeded(
                f"Refusing Apollo call: would use {self._credits_used + credits} credits, "
                f"run cap is {self.max_credits_per_run}."
            )
        self._credits_used += credits

    def _post(self, path: str, payload: dict[str, Any], *, credits: int = 0) -> dict[str, Any]:
        if credits:
            self._charge(credits)
        if self.mode == "fixture":
            return self._load_fixture(path)
        resp = requests.post(
            f"{APOLLO_HOST}{path}",
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: dict[str, Any], *, credits: int = 0) -> dict[str, Any]:
        if credits:
            self._charge(credits)
        if self.mode == "fixture":
            return self._load_fixture(path)
        resp = requests.get(
            f"{APOLLO_HOST}{path}",
            headers={"x-api-key": self.api_key},
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _load_fixture(self, path: str) -> dict[str, Any]:
        collapsed = _ID_SEGMENT.sub("/id", path)
        name = collapsed.strip("/").replace("/", "_") + ".json"
        fixture_path = (self.fixtures_dir or Path(__file__).parent.parent / "tests" / "fixtures") / name
        if not fixture_path.exists():
            raise FileNotFoundError(f"No fixture at {fixture_path} for Apollo path {path}")
        return json.loads(fixture_path.read_text())

    def search_organizations(
        self,
        keywords: list[str] | None = None,
        employee_range: tuple[int, int] | None = None,
        locations: list[str] | None = None,
        technologies: list[str] | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """Search Apollo for organizations matching ICP filters.

        `keywords` covers both industry and free-text signals — Apollo's
        organization search has one keyword/tag filter
        (q_organization_keyword_tags), not a separate structured industry-id
        filter, so translate the ICP doc's industry criteria into keywords
        here (e.g. "industrial automation", "warehouse robotics").
        """
        payload: dict[str, Any] = {"page": page, "per_page": per_page}
        if keywords:
            payload["q_organization_keyword_tags"] = keywords
        if employee_range:
            payload["organization_num_employees_ranges"] = [f"{employee_range[0]},{employee_range[1]}"]
        if locations:
            payload["organization_locations"] = locations
        if technologies:
            payload["currently_using_any_of_technology_uids"] = technologies
        return self._post("/api/v1/mixed_companies/search", payload)

    def enrich_organization(self, domain: str) -> dict[str, Any]:
        """Fetch full org detail (headcount, industry, tech stack) by domain.

        Apollo's organization enrichment endpoint looks up by domain (or
        linkedin_url/website), not by Apollo org id — pass the domain
        returned by search_organizations, not the org's id.
        """
        return self._get("/api/v1/organizations/enrich", {"domain": domain})

    def search_people(
        self,
        organization_ids: list[str],
        titles: list[str] | None = None,
        seniorities: list[str] | None = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict[str, Any]:
        """Find people at target orgs matching title/seniority filters.

        Apollo's people search has no separate department/function filter —
        express that criterion via `titles` instead.
        """
        payload: dict[str, Any] = {
            "organization_ids": organization_ids,
            "page": page,
            "per_page": per_page,
        }
        if titles:
            payload["person_titles"] = titles
        if seniorities:
            payload["person_seniorities"] = seniorities
        return self._post("/api/v1/mixed_people/api_search", payload)

    def enrich_person(
        self,
        person_id: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        organization_domain: str | None = None,
        reveal_personal_emails: bool = False,
    ) -> dict[str, Any]:
        """Resolve a verified work email + email_status for one person.

        Consumes Apollo credits — 1 credit charged against max_credits_per_run
        before the request is issued, raising ApolloCreditLimitExceeded instead
        of making the call if that would exceed the run's cap.
        """
        payload: dict[str, Any] = {"reveal_personal_emails": reveal_personal_emails}
        if person_id:
            payload["id"] = person_id
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if organization_domain:
            payload["domain"] = organization_domain
        return self._post("/api/v1/people/match", payload, credits=1)

    def bulk_enrich_people(
        self, people: list[dict[str, Any]], reveal_personal_emails: bool = False
    ) -> dict[str, Any]:
        """Batch version of enrich_person (Apollo supports up to 10/call).

        Preferred over repeated enrich_person calls for cost/latency; charges
        len(people) credits against max_credits_per_run.
        """
        if len(people) > 10:
            raise ValueError("Apollo bulk match supports at most 10 people per call")
        payload = {"details": people, "reveal_personal_emails": reveal_personal_emails}
        return self._post("/api/v1/people/bulk_match", payload, credits=len(people))
