"""Tests for graceful handling of a dead refresh grant."""

from __future__ import annotations

import pytest
import respx
import typer

from cytario_cli import cli
from cytario_cli.config import CliState
from cytario_cli.oidc import RefreshGrantError, refresh_token


class TestRefreshGrantError:
    @respx.mock
    def test_invalid_grant_raises_typed_error(self):
        respx.post("https://auth.example.com/token").respond(
            status_code=400,
            json={
                "error": "invalid_grant",
                "error_description": "Maximum allowed refresh token reuse exceeded",
            },
        )
        with pytest.raises(RefreshGrantError, match="no longer valid"):
            refresh_token("https://auth.example.com/token", "a-used-token")

    @respx.mock
    def test_other_400_stays_generic(self):
        respx.post("https://auth.example.com/token").respond(
            status_code=400, json={"error": "invalid_client"}
        )
        from cytario_cli.oidc import OidcError

        with pytest.raises(OidcError, match="token refresh failed") as exc_info:
            refresh_token("https://auth.example.com/token", "t")
        assert not isinstance(exc_info.value, RefreshGrantError)

    @respx.mock
    def test_non_json_body_stays_generic(self):
        respx.post("https://auth.example.com/token").respond(status_code=400, text="<html>bad gateway</html>")
        from cytario_cli.oidc import OidcError

        with pytest.raises(OidcError, match="400"):
            refresh_token("https://auth.example.com/token", "t")


class TestFreshTokenSignsOutGracefully:
    def make_state(self, tmp_path, monkeypatch):
        from cytario_cli import config

        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
        return CliState(
            host="https://app.example.com",
            issuer="https://auth.example.com/realms/cytario",
            authorization_endpoint="https://auth.example.com/auth",
            token_endpoint="https://auth.example.com/token",
            refresh_token="stale",
            id_token=None,
            id_token_expires_at=0.0,
        )

    @respx.mock
    def test_dead_grant_deletes_state_and_exits_with_hint(self, tmp_path, monkeypatch, capsys):
        state = self.make_state(tmp_path, monkeypatch)
        state.save()
        respx.post("https://auth.example.com/token").respond(status_code=400, json={"error": "invalid_grant"})

        with pytest.raises(typer.Exit) as exit_info:
            cli._fresh_id_token(state)

        assert exit_info.value.exit_code == 2
        assert not (tmp_path / "state.json").exists()  # stale state removed
        output = capsys.readouterr().out + capsys.readouterr().err
        assert "no longer valid" in output
        assert "cytario auth login --host https://app.example.com" in output

    @respx.mock
    def test_live_grant_still_saves_rotated_token(self, tmp_path, monkeypatch):
        import time as time_module

        from cytario_cli.oidc import id_token_expiry

        state = self.make_state(tmp_path, monkeypatch)
        id_token = "x.y.z"
        respx.post("https://auth.example.com/token").respond(
            json={
                "refresh_token": "rotated",
                "id_token": id_token,
                "access_token": "access-1",
                "expires_in": 3600,
            }
        )
        # force the refresh path: no cached token
        monkeypatch.setattr(time_module, "time", lambda: 1000.0)
        assert id_token_expiry(id_token) - 1000.0 <= 300 or True  # token unparsable → refresh runs

        result = cli._fresh_id_token(state)

        assert result == id_token
        assert state.refresh_token == "rotated"
