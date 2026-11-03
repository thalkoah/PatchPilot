"""Regression for the known defect. This test fails on the baseline checkout.

An unrecognized customer tier should be HTTP 422. The current lookup raises
KeyError, so the endpoint returns HTTP 500.
"""

from demo_service.app import app
from fastapi.testclient import TestClient


def test_unknown_tier_is_a_validation_error():
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/discount", params={"customer_tier": "platinum"})

    assert response.status_code == 422
