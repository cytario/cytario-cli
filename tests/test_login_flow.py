"""Tests for the no-browser fallback in the login flow."""

from __future__ import annotations

import pytest
import respx

from cytario_cli import oidc
from cytario_cli.oidc import OidcError, login_flow

DISCOVERY = oidc.Discovery(
    issuer="https://auth.example.com/realms/cytario",
    authorization_endpoint="https://auth.example.com/realms/cytario/protocol/openid-connect/auth",
    token_endpoint="https://auth.example.com/realms/cytario/protocol/openid-connect/token",
)


class TestNoBrowserFallback:
    @respx.mock
    def test_prints_url_and_completes_flow(self, monkeypatch, capsys):
        """With webbrowser.open failing, the URL is printed and the code still redeems."""
        monkeypatch.setattr(oidc.webbrowser, "open", lambda url: False)
        monkeypatch.setattr(oidc.LoopbackReceiver, "start", lambda self: None)
        monkeypatch.setattr(oidc.LoopbackReceiver, "wait_ready", lambda self: 54321)
        monkeypatch.setattr(oidc.LoopbackReceiver, "wait_for_code", lambda self: {"code": "the-auth-code"})

        respx.post(DISCOVERY.token_endpoint).respond(
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "id_token": "it",
                "expires_in": 3600,
                "token_type": "Bearer",
            }
        )

        tokens = login_flow(DISCOVERY)

        assert tokens["id_token"] == "it"
        output = capsys.readouterr().out
        assert "No browser available" in output
        assert DISCOVERY.authorization_endpoint in output
        assert "54321" in output  # the forwarded loopback port appears in the ssh hint
        assert b"the-auth-code" in respx.calls.last.request.content

    @respx.mock
    def test_webbrowser_true_skips_printing(self, monkeypatch, capsys):
        """When a browser opens, no fallback instructions are printed."""
        monkeypatch.setattr(oidc.webbrowser, "open", lambda url: True)
        monkeypatch.setattr(oidc.LoopbackReceiver, "start", lambda self: None)
        monkeypatch.setattr(oidc.LoopbackReceiver, "wait_ready", lambda self: 12345)

        def fail_fast(_self):
            raise OidcError("stop before waiting")

        monkeypatch.setattr(oidc.LoopbackReceiver, "wait_for_code", fail_fast)

        with pytest.raises(OidcError, match="stop"):
            login_flow(DISCOVERY)

        assert "No browser available" not in capsys.readouterr().out
