"""Tests for `cytario auth status` (host, user, token state)."""

from __future__ import annotations

import base64
import json

from cytario_cli import cli
from cytario_cli.config import CliState


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_id_token(sub: str = "e0e28a62", email: str = "user@example.com") -> str:
    header = _b64url(b'{"alg":"RS256"}')
    payload = _b64url(json.dumps({"sub": sub, "email": email, "name": "Example User"}).encode())
    return f"{header}.{payload}.{_b64url(b'signature')}"


class TestAuthStatus:
    def make_state(self, tmp_path, monkeypatch, id_token: str | None):
        from cytario_cli import config

        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
        return CliState(
            host="https://app.example.com",
            issuer="https://auth.example.com/realms/cytario",
            authorization_endpoint="https://auth.example.com/auth",
            token_endpoint="https://auth.example.com/token",
            refresh_token="grant-1",
            id_token=id_token,
            id_token_expires_at=2000000000.0,
        )

    def test_not_signed_in_json(self, tmp_path, monkeypatch, capsys):
        from cytario_cli import config

        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "missing.json")
        cli.auth_status(as_json=True)
        assert json.loads(capsys.readouterr().out) == {"signedIn": False}

    def test_json_includes_user_identity(self, tmp_path, monkeypatch, capsys):
        state = self.make_state(tmp_path, monkeypatch, make_id_token())
        state.save()
        cli.auth_status(as_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["signedIn"] is True
        assert payload["host"] == "https://app.example.com"
        assert payload["issuer"] == "https://auth.example.com/realms/cytario"
        assert payload["userId"] == "e0e28a62"
        assert payload["email"] == "user@example.com"
        assert payload["name"] == "Example User"

    def test_json_with_unparsable_token_omits_identity(self, tmp_path, monkeypatch, capsys):
        state = self.make_state(tmp_path, monkeypatch, "not-a-jwt")
        state.save()
        cli.auth_status(as_json=True)
        payload = json.loads(capsys.readouterr().out)
        assert payload["signedIn"] is True
        assert payload["userId"] is None

    def test_plain_text_prints_signed_in_as_email(self, tmp_path, monkeypatch, capsys):
        state = self.make_state(tmp_path, monkeypatch, make_id_token())
        state.save()
        cli.auth_status(as_json=False)
        output = capsys.readouterr().out
        assert "Host: https://app.example.com" in output
        assert "Signed in as: user@example.com" in output

    def test_plain_text_falls_back_to_sub_without_email(self, tmp_path, monkeypatch, capsys):
        token = make_id_token()
        header, payload, signature = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        claims.pop("email")
        rebuilt = f"{header}.{_b64url(json.dumps(claims).encode())}.{signature}"
        state = self.make_state(tmp_path, monkeypatch, rebuilt)
        state.save()
        cli.auth_status(as_json=False)
        output = capsys.readouterr().out
        assert "Signed in as: e0e28a62" in output
