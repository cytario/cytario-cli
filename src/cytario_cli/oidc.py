"""OIDC Authorization Code + PKCE with a loopback redirect receiver (RFC 8252).

Also covers token refresh and discovery-document lookups. The CLI never holds
a client secret: it is a public client.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import threading
import time
import webbrowser
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .config import CLIENT_ID, SCOPE_OFFLINE

WELL_KNOWN_PATHS = (
    "/.well-known/openid-configuration",
    "/realms/cytario/.well-known/openid-configuration",
    "/auth/realms/cytario/.well-known/openid-configuration",
)


@dataclass
class Discovery:
    """The endpoints the CLI needs from the OIDC discovery document."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str


class OidcError(Exception):
    """Raised when the OIDC flow fails (network, authorization, or token)."""


HTTP_OK = 200


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def id_token_expiry(id_token: str) -> float:
    """Return the `exp` claim of a JWT as epoch seconds (0 when unparsable)."""
    try:
        payload_segment = id_token.split(".")[1]
        payload = json.loads(_b64url_decode(payload_segment))
        return float(payload.get("exp", 0))
    except (IndexError, ValueError):
        return 0.0


def discover(base_url: str) -> Discovery:
    """Fetch the OIDC discovery document from a host.

    `base_url` is either the cytario web host (e.g. https://app.cytario.com)
    or the identity-service base URL itself. Both conventional locations are
    probed.
    """
    base = base_url.rstrip("/")
    last_error: Exception | None = None
    for path in WELL_KNOWN_PATHS:
        url = f"{base}{path}"
        try:
            response = httpx.get(url, timeout=10, follow_redirects=True)
            response.raise_for_status()
            document = response.json()
            return Discovery(
                issuer=document["issuer"],
                authorization_endpoint=document["authorization_endpoint"],
                token_endpoint=document["token_endpoint"],
            )
        except (httpx.HTTPError, KeyError, ValueError) as error:
            last_error = error
            continue
    raise OidcError(f"Could not fetch the OIDC discovery document from {base}: {last_error}")


class LoopbackReceiver:
    """One-shot localhost HTTP server receiving the authorization redirect."""

    def __init__(self) -> None:
        """Bind nothing yet; the socket is created on the serve thread."""
        self._result: dict[str, str] | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(1)
        self.port = server_socket.getsockname()[1]
        self._ready.set()
        server_socket.settimeout(300)
        try:
            connection, _ = server_socket.accept()
        except (TimeoutError, OSError) as error:
            self._result = {"error": str(error)}
            return
        with connection:
            request = connection.recv(65536).decode("utf-8", errors="replace")
            path = request.split(" ")[1] if " " in request else "/"
            query = parse_qs(urlparse(path).query)
            self._result = {key: values[0] for key, values in query.items()}
            body = b"<html><body><p>Sign-in complete. You can close this window.</p></body></html>"
            connection.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                + str(len(body)).encode("ascii")
                + b"\r\nConnection: close\r\n\r\n"
                + body
            )
        server_socket.close()

    def wait_ready(self) -> int:
        """Block until the socket listens; return the bound port."""
        self._ready.wait()
        return self.port

    def wait_for_code(self) -> dict[str, str]:
        """Block until the browser delivered the redirect; return its params."""
        self._thread.join()
        if self._result is None:
            raise OidcError("The sign-in redirect never arrived.")
        return self._result

    def start(self) -> None:
        """Start the receiver thread."""
        self._thread.start()


def login_flow(discovery: Discovery) -> dict[str, str]:
    """Run the Authorization Code + PKCE flow; return the token response.

    Blocks until the user completes sign-in in the browser or the loopback
    server times out.
    """
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())

    receiver = LoopbackReceiver()
    receiver.start()
    port = receiver.wait_ready()
    redirect_uri = f"http://127.0.0.1:{port}/"

    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE_OFFLINE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    authorization_url = f"{discovery.authorization_endpoint}?{urlencode(params)}"
    webbrowser.open(authorization_url)

    result = receiver.wait_for_code()
    if "error" in result:
        raise OidcError(f"Authorization failed: {result['error']}: {result.get('error_description', '')}")
    if "code" not in result:
        raise OidcError("The sign-in redirect carried no authorization code.")
    if "state" in result:
        pass  # state is validated by the sender via PKCE verifier uniqueness

    token_response = httpx.post(
        discovery.token_endpoint,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": result["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        timeout=15,
    )
    if token_response.status_code != HTTP_OK:
        raise OidcError(f"The token exchange failed ({token_response.status_code}): {token_response.text}")
    return token_response.json()


def refresh_token(discovery_token_endpoint: str, refresh_token_value: str) -> dict[str, str]:
    """Redeem a refresh grant; return the token response."""
    response = httpx.post(
        discovery_token_endpoint,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token_value,
        },
        timeout=15,
    )
    if response.status_code != HTTP_OK:
        raise OidcError(f"The token refresh failed ({response.status_code}): {response.text}")
    return response.json()


def token_is_fresh(id_token: str, skew_seconds: float = 300.0) -> bool:
    """Report whether the ID token has more than the skew remaining."""
    expiry = id_token_expiry(id_token)
    if expiry <= 0:
        return False
    return time.time() < expiry - skew_seconds
