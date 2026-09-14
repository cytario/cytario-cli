"""Tests for the OIDC helpers (JWT expiry, discovery, PKCE plumbing)."""

from __future__ import annotations

import base64
import time

import httpx
import pytest
import respx

from cytario_cli.oidc import Discovery, discover, id_token_expiry, token_is_fresh


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_id_token(exp: float, sub: str = "user-1") -> str:
    header = _b64url(b'{"alg":"RS256"}')
    payload = _b64url(f'{{"sub":"{sub}","exp":{exp}}}'.encode())
    return f"{header}.{payload}.{_b64url(b'signature')}"


class TestIdTokenExpiry:
    def test_reads_exp_claim(self):
        token = make_id_token(exp=2000000000)
        assert id_token_expiry(token) == 2000000000

    def test_returns_zero_on_garbage(self):
        assert id_token_expiry("not-a-jwt") == 0
        assert id_token_expiry("") == 0

    def test_token_is_fresh_with_skew(self):
        future = time.time() + 1000
        assert token_is_fresh(make_id_token(exp=future))
        soon = time.time() + 200  # under the 300s default skew
        assert not token_is_fresh(make_id_token(exp=soon))
        assert not token_is_fresh(make_id_token(exp=time.time() - 10))


class TestDiscover:
    @respx.mock
    def test_discovers_from_well_known_root(self):
        respx.get("https://app.example.com/.well-known/openid-configuration").respond(
            json={
                "issuer": "https://auth.example.com/realms/cytario",
                "authorization_endpoint": "https://auth.example.com/auth",
                "token_endpoint": "https://auth.example.com/token",
            }
        )
        discovery = discover("https://app.example.com")
        assert discovery == Discovery(
            issuer="https://auth.example.com/realms/cytario",
            authorization_endpoint="https://auth.example.com/auth",
            token_endpoint="https://auth.example.com/token",
        )

    @respx.mock
    def test_discovers_keycloak_realm_path(self):
        respx.get("https://auth.cytar.io/.well-known/openid-configuration").mock(
            side_effect=httpx.ConnectError("unreachable")
        )
        respx.get("https://auth.cytar.io/auth/realms/cytario/.well-known/openid-configuration").mock(
            side_effect=httpx.ConnectError("unreachable")
        )
        respx.get("https://auth.cytar.io/realms/cytario/.well-known/openid-configuration").respond(
            json={
                "issuer": "https://auth.cytar.io/realms/cytario",
                "authorization_endpoint": "https://auth.cytar.io/realms/cytario/protocol/openid-connect/auth",
                "token_endpoint": "https://auth.cytar.io/realms/cytario/protocol/openid-connect/token",
            }
        )
        discovery = discover("https://auth.cytar.io")
        assert discovery.issuer == "https://auth.cytar.io/realms/cytario"

    @respx.mock
    def test_falls_back_to_auth_realm_path(self):
        respx.get("https://app.example.com/.well-known/openid-configuration").mock(
            side_effect=httpx.ConnectError("no")
        )
        respx.get("https://app.example.com/realms/cytario/.well-known/openid-configuration").mock(
            side_effect=httpx.ConnectError("no")
        )
        respx.get("https://app.example.com/auth/realms/cytario/.well-known/openid-configuration").respond(
            json={
                "issuer": "https://app.example.com/auth/realms/cytario",
                "authorization_endpoint": "https://app.example.com/auth",
                "token_endpoint": "https://app.example.com/token",
            }
        )
        discovery = discover("https://app.example.com/")
        assert discovery.issuer == "https://app.example.com/auth/realms/cytario"

    @respx.mock
    def test_raises_when_unreachable(self):
        respx.route(host="app.example.com").mock(side_effect=httpx.ConnectError("down"))
        with pytest.raises(Exception, match="discovery"):
            discover("https://app.example.com")
