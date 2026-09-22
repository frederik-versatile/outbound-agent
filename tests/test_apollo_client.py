import pytest

from clients.apollo_client import ApolloClient, ApolloCreditLimitExceeded


@pytest.fixture
def client():
    return ApolloClient(api_key="fixture-mode", mode="fixture", max_credits_per_run=3)


def test_search_organizations_reads_fixture(client):
    result = client.search_organizations(keywords=["industrial automation"], employee_range=(50, 500))
    names = [org["name"] for org in result["organizations"]]
    assert "Acme Robotics" in names
    assert client.credits_used_this_run() == 0  # search is not metered


def test_enrich_organization_reads_fixture_by_domain(client):
    result = client.enrich_organization("acmerobotics.example.com")
    assert result["organization"]["name"] == "Acme Robotics"


def test_search_people_reads_fixture(client):
    result = client.search_people(organization_ids=["5f8e2a1b9c3d4e5f6a7b8c9d"], titles=["VP Sales"])
    assert any(p["name"] == "Jordan Reyes" for p in result["people"])


def test_enrich_person_charges_one_credit(client):
    client.enrich_person(person_id="7b0a4c3d1e5f6a7b8c9d0e1f")
    assert client.credits_used_this_run() == 1


def test_bulk_enrich_charges_credit_per_person(client):
    client.bulk_enrich_people([{"id": "7b0a4c3d1e5f6a7b8c9d0e1f"}, {"id": "8c1b5d4e2f6a7b8c9d0e1f2a"}])
    assert client.credits_used_this_run() == 2


def test_bulk_enrich_rejects_more_than_ten(client):
    with pytest.raises(ValueError):
        client.bulk_enrich_people([{"id": str(i)} for i in range(11)])


def test_credit_cap_is_enforced(client):
    client.enrich_person(person_id="a")
    client.enrich_person(person_id="b")
    client.enrich_person(person_id="c")
    with pytest.raises(ApolloCreditLimitExceeded):
        client.enrich_person(person_id="d")
    assert client.credits_used_this_run() == 3  # the refused call was never charged
