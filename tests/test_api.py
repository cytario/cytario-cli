"""Tests for the my-connections API client."""

from __future__ import annotations

import httpx
import pytest
import respx

from cytario_cli.api import ApiError, list_connections, serves_cytario_api

HOST = "https://app.example.com"
ENDPOINT = f"{HOST}/api/me/connections"


class TestServesCytarioApi:
    @respx.mock
    def test_web_host_answers_root(self):
        respx.head("https://app.cytar.io").respond(status_code=200)
        assert serves_cytario_api("https://app.cytar.io") is True

    @respx.mock
    def test_identity_host_404s_on_root(self):
        # Keycloak's front page sits under /realms; the bare root 404s.
        respx.head("https://auth.cytar.io").respond(status_code=404)
        assert serves_cytario_api("https://auth.cytar.io") is False

    @respx.mock
    def test_unreachable_host_is_not_serving(self):
        respx.head("https://down.example.com").mock(side_effect=httpx.ConnectError("down"))
        assert serves_cytario_api("https://down.example.com") is False


class TestListConnections:
    @respx.mock
    def test_parses_rows(self):
        respx.get(ENDPOINT).respond(
            json={
                "connections": [
                    {
                        "name": "Demo",
                        "bucketName": "demo-bucket",
                        "prefix": "",
                        "region": "us-east-1",
                        "s3Endpoint": "https://s3.us-east-1.amazonaws.com",
                        "stsEndpoint": "https://sts.us-east-1.amazonaws.com",
                        "roleArn": "arn:aws:iam::1:role/x",
                        "accessLevel": "read-only",
                    },
                    {
                        "name": "NoGrant",
                        "bucketName": "other",
                        "prefix": "p/",
                        "region": "eu-central-1",
                        "s3Endpoint": "https://s3.eu-central-1.amazonaws.com",
                        "stsEndpoint": "https://sts.eu-central-1.amazonaws.com",
                        "roleArn": None,
                        "accessLevel": None,
                    },
                ]
            }
        )
        connections = list_connections(HOST, "token")

        assert len(connections) == 2
        assert connections[0].name == "Demo"
        assert connections[0].role_arn == "arn:aws:iam::1:role/x"
        assert connections[1].role_arn is None
        assert connections[1].prefix == "p/"

    @respx.mock
    def test_sends_bearer_token(self):
        route = respx.get(ENDPOINT).respond(json={"connections": []})
        list_connections(HOST, "the-id-token")
        request = route.calls.last.request
        assert request.headers["Authorization"] == "Bearer the-id-token"

    @respx.mock
    def test_401_raises_login_hint(self):
        respx.get(ENDPOINT).respond(status_code=401, json={"error": "unauthorized"})
        with pytest.raises(ApiError, match="auth login"):
            list_connections(HOST, "stale")

    @respx.mock
    def test_other_errors_surface_status(self):
        respx.get(ENDPOINT).respond(status_code=502, text="bad gateway")
        with pytest.raises(ApiError, match="502"):
            list_connections(HOST, "token")
