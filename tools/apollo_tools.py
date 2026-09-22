"""@tool wrappers over ApolloClient, registered via create_sdk_mcp_server so
prioritization_agent and discovery_agent can call Apollo without either
having direct API access — every call still passes through ApolloClient's
own credit-limit guard regardless of what either agent's prompt says.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server

from clients.apollo_client import ApolloClient, ApolloCreditLimitExceeded

_ORG_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Industry and free-text signals — Apollo has one keyword/tag filter, "
                           "not a separate structured industry filter, so put industry terms here too.",
        },
        "employee_range": {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
        "locations": {"type": "array", "items": {"type": "string"}},
        "technologies": {"type": "array", "items": {"type": "string"}},
        "page": {"type": "integer"},
    },
}

_PEOPLE_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "organization_ids": {"type": "array", "items": {"type": "string"}},
        "titles": {"type": "array", "items": {"type": "string"}},
        "seniorities": {"type": "array", "items": {"type": "string"}},
        "page": {"type": "integer"},
    },
    "required": ["organization_ids"],
}

_ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "person_id": {"type": "string"},
        "first_name": {"type": "string"},
        "last_name": {"type": "string"},
        "organization_domain": {"type": "string"},
    },
}

_BULK_ENRICH_SCHEMA = {
    "type": "object",
    "properties": {"people": {"type": "array", "items": {"type": "object"}}},
    "required": ["people"],
}


def build_apollo_tools(client: ApolloClient) -> list:
    @tool("apollo_search_organizations", "Search Apollo for organizations matching ICP filters.", _ORG_SEARCH_SCHEMA)
    async def search_organizations(args: dict[str, Any]) -> dict[str, Any]:
        employee_range = tuple(args["employee_range"]) if args.get("employee_range") else None
        result = client.search_organizations(
            keywords=args.get("keywords"),
            employee_range=employee_range,
            locations=args.get("locations"),
            technologies=args.get("technologies"),
            page=args.get("page", 1),
        )
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool(
        "apollo_enrich_organization",
        "Fetch full detail (headcount, industry, tech stack) for one organization by its domain "
        "(the primary_domain field from apollo_search_organizations results, not its id).",
        {"domain": str},
    )
    async def enrich_organization(args: dict[str, Any]) -> dict[str, Any]:
        result = client.enrich_organization(args["domain"])
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool("apollo_search_people", "Find people at target orgs matching title/seniority filters.", _PEOPLE_SEARCH_SCHEMA)
    async def search_people(args: dict[str, Any]) -> dict[str, Any]:
        result = client.search_people(
            organization_ids=args["organization_ids"],
            titles=args.get("titles"),
            seniorities=args.get("seniorities"),
            page=args.get("page", 1),
        )
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool(
        "apollo_enrich_person",
        "Resolve a verified work email for one person. Consumes 1 Apollo credit; "
        "refuses with an error if the run's credit cap would be exceeded.",
        _ENRICH_SCHEMA,
    )
    async def enrich_person(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = client.enrich_person(
                person_id=args.get("person_id"),
                first_name=args.get("first_name"),
                last_name=args.get("last_name"),
                organization_domain=args.get("organization_domain"),
            )
        except ApolloCreditLimitExceeded as exc:
            return {"content": [{"type": "text", "text": f"ERROR: {exc}"}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @tool(
        "apollo_bulk_enrich_people",
        "Batch-resolve verified work emails for up to 10 people at once (preferred over repeated "
        "apollo_enrich_person calls). Consumes 1 credit per person; refuses if the run's cap would be exceeded.",
        _BULK_ENRICH_SCHEMA,
    )
    async def bulk_enrich_people(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = client.bulk_enrich_people(args["people"])
        except ApolloCreditLimitExceeded as exc:
            return {"content": [{"type": "text", "text": f"ERROR: {exc}"}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return [search_organizations, enrich_organization, search_people, enrich_person, bulk_enrich_people]


def apollo_server(client: ApolloClient):
    return create_sdk_mcp_server(name="apollo", tools=build_apollo_tools(client))
