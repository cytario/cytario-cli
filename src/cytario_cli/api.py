"""Client for cytario-web's /api/me/connections endpoint."""

from __future__ import annotations

import httpx

from .awsconfig import Connection

HTTP_UNAUTHORIZED = 401
HTTP_OK = 200
HTTP_CLIENT_ERROR = 400


class ApiError(Exception):
    """Raised when the my-connections endpoint fails."""


def serves_cytario_api(host: str) -> bool:
    """Report whether the host serves the Cytario web API.

    The web app answers a HEAD or GET on / with a 2xx/3xx; the identity host
    answers 404 or a Keycloak page. Used to reject identity-host sign-ins up
    front (the API would 404 later otherwise).
    """
    try:
        response = httpx.head(host.rstrip("/"), timeout=10, follow_redirects=False)
    except httpx.HTTPError:
        return False
    return response.status_code < HTTP_CLIENT_ERROR


def list_connections(host: str, id_token: str) -> list[Connection]:
    """Fetch the signed-in user's visible connections with their resolved grants."""
    url = f"{host.rstrip('/')}/api/me/connections"
    response = httpx.get(url, headers={"Authorization": f"Bearer {id_token}"}, timeout=15)
    if response.status_code == HTTP_UNAUTHORIZED:
        raise ApiError("The token was rejected. Run `cytario auth login` and try again.")
    if response.status_code != HTTP_OK:
        raise ApiError(f"GET {url} failed ({response.status_code}): {response.text}")
    payload = response.json()
    return [Connection.from_api(row) for row in payload.get("connections", [])]
