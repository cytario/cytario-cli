"""Client for cytario-web's /api/me/connections endpoint."""

from __future__ import annotations

import httpx

from .awsconfig import Connection

HTTP_UNAUTHORIZED = 401
HTTP_OK = 200


class ApiError(Exception):
    """Raised when the my-connections endpoint fails."""


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
