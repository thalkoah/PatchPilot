"""Behavior that is already correct on the unmodified demo checkout."""

from demo_service.app import app
from demo_service.discounts import KNOWN_TIERS
from fastapi.testclient import TestClient


def test_known_tiers_return_their_discount():
    client = TestClient(app, raise_server_exceptions=False)

    for tier, percent in KNOWN_TIERS.items():
        response = client.get("/discount", params={"customer_tier": tier})

        assert response.status_code == 200
        assert response.json() == {"customer_tier": tier, "percent": percent}


def test_missing_tier_is_a_validation_error():
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/discount")

    assert response.status_code == 422
