"""`cytario image describe` — image metadata via the web app's agent-describe route.

Resolves an s3 URI to one of the user's connections, runs a preflight check
that the deployment serves the `/agent/describe` route, opens the browser on
that route (under the user's own signed-in session — the CLI passes no
tokens), and receives the computed image metadata as a query parameter on a
one-shot loopback redirect. The CLI never proxies image data: the browser
talks to storage directly with its session's credentials.
"""

from __future__ import annotations

import json
import webbrowser
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlencode, urlparse

import httpx

from .oidc import (
    RECEIVER_TIMEOUT,
    REDIRECT_STATUS,
    LoopbackReceiver,
    OidcError,
)

if TYPE_CHECKING:
    from .awsconfig import Connection

MIN_AGENT_DESCRIBE_WEB = "8.5.0"

DESCRIBE_ROUTE = "/agent/describe"

DESCRIBE_DONE_TITLE = "Describe complete"
DESCRIBE_DONE_MESSAGE = "Describe complete — you can close this tab."

HTTP_OK = 200

HTTP_CLIENT_ERROR = 400

HTTP_SERVER_ERROR = 500


@dataclass
class MatchResult:
    """Outcome of matching an s3 URI against the user's connections."""

    matches: list[Connection]
    path: str  # the key with the connection prefix stripped


def parse_s3_uri(uri: str) -> tuple[str, str] | None:
    """Split `s3://<bucket>/<key...>` into (bucket, key); None when not s3."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        return None
    return parsed.netloc, parsed.path.lstrip("/")


def match_connection(connections: list[Connection], bucket: str, key: str) -> MatchResult:
    """Match a bucket+key against the connections; strip the prefix to get the path.

    A connection matches when its bucket equals the URI's bucket and the key
    starts with its prefix on a path boundary: the key equals the prefix, the
    prefix ends with a slash, or the key's character after the prefix is a
    slash (an empty prefix matches every key of the bucket).
    """

    def has_prefix(key: str, prefix: str) -> bool:
        if not prefix:
            return True
        if not key.startswith(prefix):
            return False
        return len(key) == len(prefix) or prefix.endswith("/") or key[len(prefix)] == "/"

    matches = [
        connection
        for connection in connections
        if connection.bucket_name == bucket and has_prefix(key, connection.prefix)
    ]
    longest = max((len(connection.prefix) for connection in matches), default=0)
    path = key[longest:] if matches else key
    return MatchResult(matches=matches, path=path)


def probe_describe_route(host: str) -> bool:
    """Report whether the web app serves the agent-describe route.

    The route sits behind the app's auth middleware: on a deployment that has
    it, an unauthenticated probe is redirected to the login page (30x) or
    answered with a JSON error (any 4xx); a signed-in session answering
    `?probe` with JSON also proves it. An old deployment falls through to the
    SPA fallback — HTTP 200 with an HTML body. A plain http→https upgrade
    redirect is followed once before classifying. No version information is
    disclosed anywhere; the check only distinguishes "route exists" from
    "does not".
    """
    url = f"{host.rstrip('/')}{DESCRIBE_ROUTE}?probe"
    for _ in range(2):
        try:
            response = httpx.get(url, timeout=10, follow_redirects=False)
        except httpx.HTTPError:
            return False
        if response.status_code not in REDIRECT_STATUS:
            break
        location = response.headers.get("location")
        upgraded = _scheme_upgrade(location, url)
        if not upgraded:
            return True
        url = upgraded
    else:
        return True
    if response.status_code == HTTP_OK:
        return response.headers.get("content-type", "").startswith("application/json")
    if HTTP_CLIENT_ERROR <= response.status_code < HTTP_SERVER_ERROR:
        return response.headers.get("content-type", "").startswith("application/json")
    return False


def _scheme_upgrade(location: str | None, url: str) -> str | None:
    """Return the redirect target when it upgrades http to https on the same path."""
    if not location:
        return None
    parsed = urlparse(location)
    original = urlparse(url)
    if (
        parsed.scheme == "https"
        and original.scheme == "http"
        and (parsed.netloc, parsed.path)
        == (
            original.netloc,
            original.path,
        )
    ):
        return location
    return None


def describe_url(host: str, connection_id: str, path: str, port: int) -> str:
    """Build the describe-route URL the browser opens, loopback port included."""
    query = urlencode({"connectionId": connection_id, "path": path, "port": port})
    return f"{host.rstrip('/')}{DESCRIBE_ROUTE}?{query}"


def describe_flow(host: str, connection_id: str, path: str) -> str:
    """Run the browser round trip; return the pretty-printed payload for stdout.

    Starts a one-shot loopback receiver, opens the describe route in the
    browser, and waits for the result redirect. The CLI passes no tokens:
    the route runs under the user's own signed-in session.
    """
    receiver = LoopbackReceiver(done_title=DESCRIBE_DONE_TITLE, done_message=DESCRIBE_DONE_MESSAGE)
    receiver.start()
    port = receiver.wait_ready()

    url = describe_url(host, connection_id, path, port)
    opened = webbrowser.open(url)
    if not opened:
        print("No browser available on this machine.")
        print()
        print("1. Forward this machine's loopback port to a device with a browser, e.g.:")
        print(f"   ssh -L {port}:127.0.0.1:{port} <this-host>")
        print("2. Then open this URL there:")
        print()
        print(url)
        print()
        print(f"Waiting for the result redirect on 127.0.0.1:{port} ... (Ctrl+C to cancel)")

    try:
        result = receiver.wait_for_code()
    except OidcError as error:
        raise OidcError("The result redirect never arrived.") from error
    if result.get("error") == RECEIVER_TIMEOUT:
        raise OidcError("The result redirect never arrived.")
    if "error" in result:
        raise OidcError(f"The describe route reported an error: {result['error']}")
    if "payload" not in result:
        raise OidcError("The result redirect carried no payload.")
    try:
        payload = json.loads(result["payload"])
    except ValueError as error:
        raise OidcError("The result payload is not valid JSON.") from error
    return json.dumps(payload, indent=2)
