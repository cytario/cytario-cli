"""Tests for access-token persistence and use for API calls."""

from __future__ import annotations

import pytest
import respx
from typer.testing import CliRunner

from cytario_cli import cli
from cytario_cli.config import CliState

runner = CliRunner()

STATE = CliState(
    host="https://app.example.com",
    issuer="https://auth.example.com/realms/cytario",
    authorization_endpoint="https://auth.example.com/auth",
    token_endpoint="https://auth.example.com/token",
    refresh_token="grant",
    id_token=None,
    id_token_expires_at=0.0,
    access_token=None,
    access_token_expires_at=0.0,
)

CONNECTION = {
    "name": "Demo",
    "bucketName": "demo-bucket",
    "prefix": "",
    "region": "eu-central-1",
    "s3Endpoint": "https://s3.eu-central-1.amazonaws.com",
    "stsEndpoint": "https://sts.eu-central-1.amazonaws.com",
    "roleArn": "arn:aws:iam::1:role/x",
    "accessLevel": "read-only",
}


@pytest.fixture(name="patched_state")
def _patched_state(tmp_path, monkeypatch):
    from cytario_cli import config

    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    state = STATE.__class__(**STATE.__dict__)
    state.save()
    return state


class TestAccessToken:
    @respx.mock
    def test_refresh_persists_access_token(self, patched_state):
        route = respx.post("https://auth.example.com/token").respond(
            json={
                "refresh_token": "rotated",
                "id_token": "header.e30.signature",
                "access_token": "the-access-token",
                "expires_in": 300,
            }
        )
        assert cli._fresh_access_token(patched_state) == "the-access-token"
        # The rotated refresh token and the access token both survive a reload.
        reloaded = CliState.load()
        assert reloaded.refresh_token == "rotated"
        assert reloaded.access_token == "the-access-token"
        assert route.called

    @respx.mock
    def test_connections_list_sends_access_token(self, patched_state, monkeypatch):
        token_route = respx.post("https://auth.example.com/token").respond(
            json={
                "refresh_token": "rotated",
                "id_token": "header.e30.signature",
                "access_token": "the-access-token",
                "expires_in": 300,
            }
        )
        api_route = respx.get("https://app.example.com/api/me/connections").respond(
            json={"connections": [CONNECTION]}
        )
        monkeypatch.setattr(cli, "serves_cytario_api", lambda host: True)

        result = runner.invoke(cli.app, ["connections", "list", "--json"])

        assert result.exit_code == 0, result.output
        request = api_route.calls.last.request
        assert request.headers["Authorization"] == "Bearer the-access-token"
        assert token_route.called

    @respx.mock
    def test_cached_access_token_skips_refresh(self, patched_state):
        import time as time_module

        patched_state.access_token = "still-valid"
        patched_state.access_token_expires_at = time_module.time() + 1000
        patched_state.id_token = "header.e30.signature"
        patched_state.id_token_expires_at = time_module.time() + 1000

        refresh_route = respx.post("https://auth.example.com/token")

        assert cli._fresh_access_token(patched_state) == "still-valid"
        assert not refresh_route.called
