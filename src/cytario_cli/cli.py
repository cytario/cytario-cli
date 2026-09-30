"""Typer CLI entry point.

Commands:
  auth login [--host <url>]        Browser sign-in (Authorization Code + PKCE,
                                   loopback redirect). Stores the refresh grant
                                   user-private.
  auth token                       Print a fresh ID token (for scripts and agents).
  auth refresh                     Refresh all managed token files.
  auth status [--json]             Show the signed-in host, user (Keycloak sub),
                                   and token state.
  connections list [--json]       List the user's connections with their grants.
  connections setup [--all | NAME] Write AWS CLI profiles + token files.
  skill list [--json]             Show detected AI tools and skill install state.
  skill install [--tool ID] ...   Install or update the packaged agent skill
                                   into the detected AI tools' directories.
  image describe S3_URI [--host]  Print an image's metadata + contrast limits
                                   as JSON (browser round trip, loopback result).

Host selection order: --host flag, CYTARIO_HOST environment variable, the
persisted default from the last login. The host is the cytario WEB app
(e.g. https://app.cytario.com) — not the identity host; the identity endpoints
are derived from it automatically.

Usage:
  cytario auth login --host https://app.cytario.com
  cytario connections list --json
  cytario connections setup --all
  cytario image describe s3://bucket/slide.ome.tif
  aws s3 ls --profile cytario-mybucket
"""

from __future__ import annotations

import json as json_module
import os
import sys
import time
from pathlib import Path  # noqa: TC003  # Typer resolves option annotations at runtime
from typing import Annotated

import typer

from . import __version__
from .api import ApiError, list_connections, serves_cytario_api
from .awsconfig import Connection, write_profile
from .config import CliState, write_token_file
from .imagedescribe import (
    MIN_AGENT_DESCRIBE_WEB,
    describe_flow,
    match_connection,
    parse_s3_uri,
    probe_describe_route,
)
from .oidc import (
    OidcError,
    RefreshGrantError,
    discover,
    id_token_claims,
    id_token_expiry,
    login_flow,
    refresh_token,
)
from .skill import (
    ToolTarget,
    detect_tools,
    install_skill,
    install_status,
    packaged_skill,
    skill_file,
)

app = typer.Typer(
    help="Work with Cytario storage connections as the signed-in user.",
    no_args_is_help=True,
)
auth_app = typer.Typer(help="Sign in, tokens, and sign-out state.", no_args_is_help=True)
connections_app = typer.Typer(help="List connections and set up AWS CLI profiles.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(connections_app, name="connections")
skill_app = typer.Typer(help="Install and update the packaged agent skill.", no_args_is_help=True)
app.add_typer(skill_app, name="skill")
image_app = typer.Typer(help="Read image metadata through the web app.", no_args_is_help=True)
app.add_typer(image_app, name="image")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"cytario {__version__}")
        raise typer.Exit


def _resolve_host(host: str | None) -> str:
    if host:
        return host.rstrip("/")
    env_host = os.environ.get("CYTARIO_HOST")
    if env_host:
        return env_host.rstrip("/")
    state = CliState.load()
    if state:
        return state.host
    typer.secho(
        "No host configured. Pass --host or run `cytario auth login --host <url>`.", fg=typer.colors.RED
    )
    raise typer.Exit(code=2)


def _load_state() -> CliState:
    state = CliState.load()
    if not state:
        typer.secho("Not signed in. Run `cytario auth login --host <url>`.", fg=typer.colors.RED)
        raise typer.Exit(code=2)
    return state


def _fresh_id_token(state: CliState, min_validity: float = 300.0) -> str:
    """Return an ID token with at least min_validity seconds left, refreshing as needed."""
    if state.id_token and id_token_expiry(state.id_token) - time.time() > min_validity:
        return state.id_token
    typer.echo("Refreshing tokens...")
    try:
        tokens = refresh_token(state.token_endpoint, state.refresh_token)
    except RefreshGrantError:
        state.delete()
        typer.secho(
            "Your saved sign-in is no longer valid. "
            f"Run `cytario auth login --host {state.host}` to sign in again.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2) from None
    _store_refreshed_tokens(state, tokens)
    return state.id_token


def _store_refreshed_tokens(state: CliState, tokens: dict[str, str]) -> None:
    """Persist a token response (rotation, both token types, expiries)."""
    state.refresh_token = tokens["refresh_token"]
    state.id_token = tokens["id_token"]
    state.id_token_expires_at = id_token_expiry(tokens["id_token"])
    if tokens.get("access_token"):
        state.access_token = tokens["access_token"]
        state.access_token_expires_at = time.time() + float(tokens.get("expires_in", 0))
    state.save()


def _fresh_access_token(state: CliState, min_validity: float = 300.0) -> str:
    """Return an access token with at least min_validity seconds left, refreshing as needed.

    The my-connections endpoint authenticates with and forwards this token:
    the portal catalog lookups exchange it (RFC 8693) to resolve the org —
    the ID token is not exchangable.
    """
    if state.access_token and state.access_token_expires_at - time.time() > min_validity:
        return state.access_token
    # Reuse the ID-token path: it refreshes both tokens in one grant call.
    _fresh_id_token(state)
    if not state.access_token:
        typer.secho(
            "No access token available. Run `cytario auth login` to sign in again.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)
    return state.access_token


@auth_app.command("login")
def auth_login(
    host: Annotated[
        str | None,
        typer.Option(help="Cytario web host, e.g. https://app.cytario.com (not the identity host)"),
    ] = None,
) -> None:
    """Sign in through the browser (Authorization Code + PKCE)."""
    resolved_host = _resolve_host(host)
    typer.echo(f"Signing in to {resolved_host}...")
    try:
        discovery = discover(resolved_host)
    except OidcError as error:
        typer.secho(f"Sign-in failed: {error}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from error
    # The web host serves the API; the identity host does not. A host whose
    # OIDC document resolved but that serves no Cytario API is the identity
    # host — the API calls would 404 later, so refuse it now.
    if not serves_cytario_api(resolved_host):
        typer.secho(
            f"{resolved_host} does not serve the Cytario API — it looks like the identity host. "
            "Sign in with the cytario web host instead, e.g. https://app.cytario.com.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)
    try:
        tokens = login_flow(discovery)
    except OidcError as error:
        typer.secho(f"Sign-in failed: {error}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from error
    state = CliState(
        host=resolved_host,
        issuer=discovery.issuer,
        authorization_endpoint=discovery.authorization_endpoint,
        token_endpoint=discovery.token_endpoint,
        refresh_token=tokens["refresh_token"],
        id_token=tokens["id_token"],
        id_token_expires_at=id_token_expiry(tokens["id_token"]),
        access_token=tokens.get("access_token"),
        access_token_expires_at=time.time() + float(tokens.get("expires_in", 0))
        if tokens.get("access_token")
        else 0.0,
    )
    state.save()
    typer.secho(f"Signed in to {resolved_host}.", fg=typer.colors.GREEN)


@auth_app.command("token")
def auth_token() -> None:
    """Print a fresh ID token to stdout."""
    state = _load_state()
    typer.echo(_fresh_id_token(state))


@auth_app.command("refresh")
def auth_refresh() -> None:
    """Refresh every managed token file to a current ID token."""
    state = _load_state()
    access_token = _fresh_access_token(state)
    id_token = _fresh_id_token(state)
    _refresh_token_files(state.host, access_token, id_token)


def _refresh_token_files(host: str, access_token: str, id_token: str) -> None:
    try:
        connections = list_connections(host, access_token)
    except ApiError as error:
        typer.secho(f"Could not list connections: {error}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from error
    for connection in connections:
        if connection.role_arn:
            write_token_file(connection.slug, id_token)
    typer.secho(f"Refreshed {len(connections)} token file(s).", fg=typer.colors.GREEN)


@auth_app.command("status")
def auth_status(
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON")] = False,
) -> None:
    """Show the signed-in host, user, and token state."""
    state = CliState.load()
    if not state:
        if as_json:
            typer.echo(json_module.dumps({"signedIn": False}, indent=2))
        else:
            typer.echo("Not signed in.")
        return
    claims = id_token_claims(state.id_token) if state.id_token else {}
    remaining = state.id_token_expires_at - time.time() if state.id_token_expires_at else 0
    if as_json:
        typer.echo(
            json_module.dumps(
                {
                    "signedIn": True,
                    "host": state.host,
                    "issuer": state.issuer,
                    "userId": claims.get("sub"),
                    "email": claims.get("email"),
                    "name": claims.get("name"),
                    "idTokenExpiresIn": max(remaining, 0),
                },
                indent=2,
            )
        )
        return
    typer.echo(f"Host: {state.host}")
    typer.echo(f"Issuer: {state.issuer}")
    if claims.get("email"):
        typer.echo(f"Signed in as: {claims['email']}")
    elif claims.get("sub"):
        typer.echo(f"Signed in as: {claims['sub']}")
    typer.echo(f"ID token expires in: {max(remaining, 0):.0f}s" if state.id_token else "No ID token cached")


@connections_app.command("list")
def connections_list(
    host: Annotated[str | None, typer.Option(help="Cytario host")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON")] = False,
) -> None:
    """List the user's visible connections with their resolved grants."""
    state = _load_state()
    resolved_host = _resolve_host(host) if host else state.host
    access_token = _fresh_access_token(state)
    try:
        connections = list_connections(resolved_host, access_token)
    except ApiError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    if as_json:
        typer.echo(
            json_module.dumps(
                [
                    {
                        "name": connection.name,
                        "bucketName": connection.bucket_name,
                        "prefix": connection.prefix,
                        "region": connection.region,
                        "s3Endpoint": connection.s3_endpoint,
                        "stsEndpoint": connection.sts_endpoint,
                        "roleArn": connection.role_arn,
                        "accessLevel": connection.access_level,
                    }
                    for connection in connections
                ],
                indent=2,
            )
        )
        return
    if not connections:
        typer.echo("No connections visible to you.")
        return
    for connection in connections:
        grant = (
            f"{connection.access_level} ({connection.role_arn})"
            if connection.role_arn
            else "no applicable grant"
        )
        typer.echo(
            f"{connection.name}  bucket={connection.bucket_name}  region={connection.region}  grant={grant}"
        )


@connections_app.command("setup")
def connections_setup(
    setup_all: Annotated[bool, typer.Option("--all", help="Set up every connection with a grant")] = False,
    name: Annotated[
        str | None, typer.Argument(help="Connection name (defaults to --all when omitted)")
    ] = None,
    host: Annotated[str | None, typer.Option(help="Cytario host")] = None,
) -> None:
    """Write an AWS CLI profile (web_identity_token_file) for each connection."""
    state = _load_state()
    resolved_host = _resolve_host(host) if host else state.host
    access_token = _fresh_access_token(state)
    id_token = _fresh_id_token(state)
    try:
        connections = list_connections(resolved_host, access_token)
    except ApiError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    if not connections:
        typer.secho("No connections are visible to you on this host.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    usable = [connection for connection in connections if connection.role_arn]
    if not usable:
        typer.secho(
            "Connections are visible, but none has a grant applicable to you — "
            "ask an organization admin for access (or check `cytario connections list`).",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    if name:
        usable = [connection for connection in usable if connection.name == name]
        if not usable:
            typer.secho(f"No connection named {name!r} with an applicable grant.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
    elif not setup_all:
        typer.echo("No connection selected; use --all or pass a connection name.")
        raise typer.Exit(code=2)

    for connection in usable:
        token_file = write_token_file(connection.slug, id_token)
        profile = write_profile(connection, token_file)
        typer.secho(
            f"{connection.name}: profile {profile!r} ready (token {token_file}).", fg=typer.colors.GREEN
        )


@skill_app.command("list")
def skill_list(
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON")] = False,
) -> None:
    """Show detected AI tools and whether the cytario skill is installed for each."""
    tools = detect_tools()
    packaged = packaged_skill()
    if as_json:
        typer.echo(
            json_module.dumps(
                [
                    {
                        "tool": tool.id,
                        "name": tool.name,
                        "skillsDir": str(tool.skills_dir),
                        "skillFile": str(skill_file(tool)),
                        "status": install_status(tool, packaged),
                    }
                    for tool in tools
                ],
                indent=2,
            )
        )
        return
    if not tools:
        typer.echo("No known AI tool detected. Pass --path to install the skill into a custom directory.")
        return
    for tool in tools:
        typer.echo(f"{tool.name}  {skill_file(tool)}  [{install_status(tool, packaged)}]")


@skill_app.command("install")
def skill_install(
    tool_id: Annotated[
        str | None,
        typer.Option("--tool", help="Install for one tool id (see `cytario skill list`)"),
    ] = None,
    path: Annotated[
        Path | None,
        typer.Option("--path", help="Install into this directory instead of a detected tool's"),
    ] = None,
    force: Annotated[bool, typer.Option("--force", help="Overwrite a locally modified skill copy")] = False,
) -> None:
    """Install or update the packaged agent skill into your AI tools' directories."""
    targets: list[ToolTarget]
    if path:
        targets = [ToolTarget("custom", "Custom directory", path, path, True)]
    else:
        targets = detect_tools()
        if tool_id:
            targets = [tool for tool in targets if tool.id == tool_id]
            if not targets:
                typer.secho(
                    f"No detected tool with id {tool_id!r}. Run `cytario skill list` to see ids.",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(code=2)
    if not targets:
        typer.secho(
            "No AI tool detected. Pass --path <dir> to install the skill manually.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    packaged = packaged_skill()
    for tool in targets:
        result = install_skill(tool, packaged, force=force)
        destination = skill_file(tool)
        if result == "up-to-date":
            typer.secho(f"{tool.name}: {destination} already up to date.", fg=typer.colors.GREEN)
        elif result == "stale-unforced":
            typer.secho(
                f"{tool.name}: {destination} differs from the packaged skill "
                "(locally modified or outdated). Re-run with --force to overwrite.",
                fg=typer.colors.YELLOW,
            )
        else:
            verb = "written to" if result == "installed" else "updated at"
            typer.secho(f"{tool.name}: skill {verb} {destination}.", fg=typer.colors.GREEN)


def _print_connection_candidates(connections: list[Connection]) -> None:
    if not connections:
        typer.echo("No connections visible to you.")
        return
    for connection in connections:
        prefix = connection.prefix or "(none)"
        typer.echo(
            f"  {connection.name}  bucket={connection.bucket_name}  prefix={prefix}  "
            f"accessLevel={connection.access_level or 'n/a'}"
        )


@image_app.command("describe")
def image_describe(
    s3_uri: Annotated[str, typer.Argument(help="Image URI, e.g. s3://bucket/prefix/slide.ome.tif")],
    host: Annotated[str | None, typer.Option(help="Cytario host")] = None,
) -> None:
    """Print an image's metadata and per-channel contrast limits as JSON.

    Opens the web app's agent-describe route in the browser (under the user's
    signed-in session — no tokens pass through the CLI) and receives the
    computed payload on a loopback redirect.
    """
    parsed = parse_s3_uri(s3_uri)
    if not parsed:
        typer.secho(
            f"Not an s3 URI: {s3_uri!r} — expected s3://<bucket>/<key>.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)
    bucket, key = parsed

    state = _load_state()
    resolved_host = _resolve_host(host) if host else state.host
    access_token = _fresh_access_token(state)
    try:
        connections = list_connections(resolved_host, access_token)
    except ApiError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    result = match_connection(connections, bucket, key)
    if not result.matches:
        typer.secho(
            f"No connection matches s3://{bucket}/{key}. Connections visible to you:",
            fg=typer.colors.RED,
        )
        _print_connection_candidates(connections)
        raise typer.Exit(code=1)
    if len(result.matches) > 1:
        typer.secho(f"s3://{bucket}/{key} matches more than one connection:", fg=typer.colors.RED)
        _print_connection_candidates(result.matches)
        raise typer.Exit(code=1)
    connection = result.matches[0]

    if not probe_describe_route(resolved_host):
        typer.secho(
            f"The web app at {resolved_host} predates the agent-describe route. "
            f"cytario image describe requires cytario-web >= {MIN_AGENT_DESCRIBE_WEB}.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    if not connection.id:
        typer.secho(
            f"The web app at {resolved_host} predates the connection-id field "
            "(its /api/me/connections response carries no id). "
            f"cytario image describe requires cytario-web >= {MIN_AGENT_DESCRIBE_WEB}.",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    try:
        payload = describe_flow(resolved_host, connection.id, result.path)
    except OidcError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error
    typer.echo(payload)


@app.callback()
def main(
    _version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, help="Show the version and exit", is_eager=True
        ),
    ] = False,
) -> None:
    """Work with Cytario storage connections as the signed-in user."""


if __name__ == "__main__":
    sys.exit(app())
