"""OIDC Authorization Code + PKCE with a loopback redirect receiver (RFC 8252).

Also covers token refresh and discovery lookups. The CLI never holds a
client secret: it is a public client.

The user-facing host is the cytario WEB app (e.g. https://app.cytar.io) —
the origin that serves /api/me/connections. Identity endpoints are derived
from it: either the host proxies a discovery document, or (the normal
deployment shape) the web app's /login route redirects to the identity
service's authorization endpoint, from which the issuer and token endpoint
are derived.
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

REDIRECT_STATUS = (301, 302, 303, 307, 308)

AUTH_PATH_MARKER = "/protocol/openid-connect/auth"


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
    """Resolve the OIDC endpoints from the cytario host.

    `base_url` is the cytario web host (e.g. https://app.cytar.io); an
    identity-service base URL also works. Well-known paths are probed first;
    when none respond (the web app proxies no discovery document), the web
    app's /login redirect to the authorization endpoint supplies the identity
    service, and the issuer/token endpoint follow the realm layout.
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
    try:
        return _discover_via_login_redirect(base)
    except (OidcError, httpx.HTTPError) as error:
        raise OidcError(
            f"Could not resolve the identity service from {base}: {last_error}; {error}"
        ) from error


def _discover_via_login_redirect(base: str) -> Discovery:
    """Derive the identity endpoints from the web app's /login redirect.

    GET {base}/login answers a redirect whose Location is the authorization
    endpoint; the realm base is that URL with the Keycloak authorization path
    stripped, and the token endpoint sits alongside it.
    """
    response = httpx.get(f"{base}/login", timeout=10, follow_redirects=False)
    location = response.headers.get("location")
    if response.status_code not in REDIRECT_STATUS or not location:
        raise OidcError(f"GET {base}/login did not redirect to the identity service ({response.status_code})")
    authorization_endpoint = location.split("?")[0]
    if not authorization_endpoint.endswith(AUTH_PATH_MARKER):
        raise OidcError(
            f"The login redirect does not point at an OIDC authorization endpoint: {authorization_endpoint}"
        )
    issuer = authorization_endpoint[: -len(AUTH_PATH_MARKER)]
    token_endpoint = f"{issuer}/protocol/openid-connect/token"
    return Discovery(
        issuer=issuer,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
    )


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
    opened = webbrowser.open(authorization_url)
    if not opened:
        # No browser on this machine (headless workstation, container, SSH
        # session). The authorization URL can be opened on any device, but the
        # sign-in completes only when the browser can redirect back to this
        # machine's loopback — on a remote/workspace host that requires
        # forwarding the loopback port to the browsing device (e.g. SSH
        # -L / Coder port-forward of this port) first.
        print("No browser available on this machine.")
        print()
        print("1. Forward this machine's loopback port to a device with a browser, e.g.:")
        print(f"   ssh -L {port}:127.0.0.1:{port} <this-host>")
        print("2. Then open this URL there:")
        print()
        print(authorization_url)
        print()
        print(f"Waiting for the sign-in redirect on 127.0.0.1:{port} ... (Ctrl+C to cancel)")

    result = receiver.wait_for_code()
    if "error" in result:
        raise OidcError(f"Authorization failed: {result['error']}: {result.get('error_description', '')}")
    if "code" not in result:
        raise OidcError("The sign-in redirect carried no authorization code.")

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
