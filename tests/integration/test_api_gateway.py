"""Integration tests for the API Gateway endpoint.

These tests require a deployed stack. They are skipped automatically when the
stack cannot be found, so the standard ``pytest`` run (unit tests) stays green
without a live deployment. To run integration tests explicitly:

    pytest tests/integration/ -c region=us-east-1

The stack name is read from the ``AWS_BACKEND_STACK_NAME`` environment variable
(set in pyproject.toml). Override it for a different region:

    AWS_BACKEND_STACK_NAME=HelloWorld-ap-southeast-1 pytest tests/integration/
"""

import os
import uuid

import boto3
import pytest
import requests


def _idempotency_headers() -> dict[str, str]:
    """Fresh Idempotency-Key per call so each request is treated as new.

    A real client should reuse the same key across automatic retries of one
    logical request; tests just need uniqueness so replay-cache hits don't
    confound assertions.
    """
    return {"Idempotency-Key": str(uuid.uuid4())}


@pytest.fixture
def api_gateway_url():
    """Get the API Gateway URL from CloudFormation stack outputs.

    Module-scoped so both TestApiGateway and TestNbaRoutes can consume it.
    Skips the test if the stack is not deployed rather than failing, so
    the test suite stays green in environments without a live deployment.
    """
    stack_name = os.environ.get("AWS_BACKEND_STACK_NAME")

    if stack_name is None:
        pytest.skip("AWS_BACKEND_STACK_NAME not set — skipping integration tests")

    client = boto3.client("cloudformation")

    try:
        response = client.describe_stacks(StackName=stack_name)
    except Exception:
        pytest.skip(f"Stack '{stack_name}' not found — skipping integration tests")

    stacks = response["Stacks"]
    stack_outputs = stacks[0]["Outputs"]
    api_outputs = [output for output in stack_outputs if output["OutputKey"] == "HelloWorldApiOutput"]

    if not api_outputs:
        pytest.skip(f"HelloWorldApiOutput not found in stack '{stack_name}' — skipping integration tests")

    return api_outputs[0]["OutputValue"]


class TestApiGateway:
    def test_api_gateway(self, api_gateway_url):
        """Call the API Gateway endpoint and check the response"""
        response = requests.get(api_gateway_url, timeout=10, headers=_idempotency_headers())

        assert response.status_code == 200
        assert response.json() == {"message": "hello world"}

    def test_api_gateway_response_headers(self, api_gateway_url):
        """Verify the response returns correct content type"""
        response = requests.get(api_gateway_url, timeout=10, headers=_idempotency_headers())

        assert response.headers["Content-Type"] == "application/json"

    def test_api_gateway_response_time_warm(self, api_gateway_url):
        """Warm-path latency budget — a P50 ceiling, not a timeout proxy.

        A first call warms the container; a second call asserts the warm-path
        budget. ``response.elapsed`` is the request-side timing as measured by
        ``requests`` (network + server), so this is a black-box ceiling rather
        than a backend SLO.
        """
        # warm-up
        requests.get(api_gateway_url, timeout=10, headers=_idempotency_headers())
        response = requests.get(api_gateway_url, timeout=10, headers=_idempotency_headers())

        assert response.status_code == 200
        assert response.elapsed.total_seconds() < 2.0

    def test_missing_idempotency_key_returns_400(self, api_gateway_url):
        """The Lambda requires Idempotency-Key — calls without it return 400."""
        response = requests.get(api_gateway_url, timeout=10)

        assert response.status_code == 400
        assert "Idempotency-Key" in response.text


class TestNbaRoutes:
    """Post-deploy smoke tests for the NBA player routes.

    Anchored on LeBron James as the canonical fixture player — he appears in
    the dataset and his record is stable. The tests assert the wire contract
    (status, content-type, key fields) rather than specific stat values, so a
    dataset refresh that adds more recent seasons does not break the suite.
    """

    LEBRON_NAME = "LeBron James"

    @pytest.fixture
    def api_base(self, api_gateway_url):
        # HelloWorldApiOutput ends in "/Prod/hello"; strip the trailing path to
        # get the root from which other routes hang.
        return api_gateway_url.rsplit("/", 1)[0]

    def test_list_players_returns_non_empty_payload(self, api_base):
        """``GET /players`` returns the populated catalog after import."""
        response = requests.get(f"{api_base}/players", timeout=20, headers=_idempotency_headers())

        assert response.status_code == 200
        body = response.json()
        # The dataset has ~2,551 distinct names; the importer keys profiles
        # by (name, college, draft_year, draft_number) so the count is
        # slightly higher. A floor of 2,000 catches accidental empty imports
        # without coupling the test to an exact count.
        assert body["count"] >= 2_000
        assert isinstance(body["players"], list)
        assert len(body["players"]) == body["count"]
        first = body["players"][0]
        assert "player_id" in first
        assert "player_name" in first

    def test_get_player_returns_full_career(self, api_base):
        """``GET /players/{id}`` returns profile + seasons for a known player."""
        listing = requests.get(
            f"{api_base}/players",
            timeout=20,
            headers=_idempotency_headers(),
        ).json()
        matches = [p for p in listing["players"] if p["player_name"] == self.LEBRON_NAME]
        assert matches, f"{self.LEBRON_NAME} missing from /players"
        player_id = matches[0]["player_id"]

        response = requests.get(
            f"{api_base}/players/{player_id}",
            timeout=10,
            headers=_idempotency_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["player_name"] == self.LEBRON_NAME
        # 2003 entry → at least 20 seasons in this dataset window
        assert len(body["seasons"]) >= 20
        rookie = next(s for s in body["seasons"] if s["season"] == "2003-04")
        assert rookie["team_abbreviation"] == "CLE"
        assert rookie["pts"] is not None

    def test_get_player_returns_404_for_unknown_id(self, api_base):
        """A bogus player_id returns 404 (not 500)."""
        response = requests.get(
            f"{api_base}/players/00000000-0000-0000-0000-000000000000",
            timeout=10,
            headers=_idempotency_headers(),
        )
        assert response.status_code == 404
