"""Typer CLI entry point.

Commands:
  auth login [--host <url>]        Browser sign-in (Authorization Code + PKCE,
                                   loopback redirect). Stores the refresh grant
                                   user-private.
  auth token                       Print a fresh ID token (for scripts and agents).
  auth refresh                     Refresh all managed token files.
  auth status                      Show the signed-in host and token state.
  connections list [--json]       List the user's connections with their grants.
  connections setup [--all | NAME] Write AWS CLI profiles + token files.

Host selection order: --host flag, CYTARIO_HOST environment variable, the
persisted default from the last login. The host is the cytario WEB app
(e.g. https://app.cytar.io) — not the identity host; the identity endpoints
are derived from it automatically.

Usage:
  cytario auth login --host https://app.cytar.io
  cytario connections list --json
  cytario connections setup --all
  aws s3 ls --profile cytario-mybucket
"""

from __future__ import annotations

import json as json_module
import os
import sys
import time
from typing import Annotated

import typer

from . import __version__
from .api import ApiError, list_connections, serves_cytario_api
from .awsconfig import write_profile
from .config import CliState, write_token_file
from .oidc import (
    OidcError,
    RefreshGrantError,
    discover,
    id_token_expiry,
    login_flow,
    refresh_token,
)

app = typer.Typer(
    help="Work with Cytario storage connections as the signed-in user.",
    no_args_is_help=True,
)
auth_app = typer.Typer(help="Sign in, tokens, and sign-out state.", no_args_is_help=True)
connections_app = typer.Typer(help="List connections and set up AWS CLI profiles.", no_args_is_help=True)
app.add_typer(auth_app, name="auth")
app.add_typer(connections_app, name="connections")


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
    typer.echo("Refreshing the ID token...")
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
    state.refresh_token = tokens["refresh_token"]
    state.id_token = tokens["id_token"]
    state.id_token_expires_at = id_token_expiry(tokens["id_token"])
    state.save()
    return state.id_token


@auth_app.command("login")
def auth_login(
    host: Annotated[
        str | None, typer.Option(help="Cytario web host, e.g. https://app.cytar.io (not the identity host)")
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
            "Sign in with the cytario web host instead, e.g. https://app.cytar.io.",
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
    id_token = _fresh_id_token(state)
    _refresh_token_files(state.host, id_token)


def _refresh_token_files(host: str, id_token: str) -> None:
    try:
        connections = list_connections(host, id_token)
    except ApiError as error:
        typer.secho(f"Could not list connections: {error}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from error
    for connection in connections:
        if connection.role_arn:
            write_token_file(connection.slug, id_token)
    typer.secho(f"Refreshed {len(connections)} token file(s).", fg=typer.colors.GREEN)


@auth_app.command("status")
def auth_status() -> None:
    """Show the signed-in host and token state."""
    state = CliState.load()
    if not state:
        typer.echo("Not signed in.")
        return
    remaining = state.id_token_expires_at - time.time() if state.id_token_expires_at else 0
    typer.echo(f"Host: {state.host}")
    typer.echo(f"Issuer: {state.issuer}")
    typer.echo(f"ID token expires in: {max(remaining, 0):.0f}s" if state.id_token else "No ID token cached")


@connections_app.command("list")
def connections_list(
    host: Annotated[str | None, typer.Option(help="Cytario host")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON")] = False,
) -> None:
    """List the user's visible connections with their resolved grants."""
    state = _load_state()
    resolved_host = _resolve_host(host) if host else state.host
    id_token = _fresh_id_token(state)
    try:
        connections = list_connections(resolved_host, id_token)
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
    id_token = _fresh_id_token(state)
    try:
        connections = list_connections(resolved_host, id_token)
    except ApiError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    usable = [connection for connection in connections if connection.role_arn]
    if name:
        usable = [connection for connection in usable if connection.name == name]
        if not usable:
            typer.secho(f"No connection named {name!r} with an applicable grant.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
    elif not setup_all and usable:
        typer.echo("No connection selected; use --all or pass a connection name.")
        raise typer.Exit(code=2)

    for connection in usable:
        token_file = write_token_file(connection.slug, id_token)
        profile = write_profile(connection, token_file)
        typer.secho(
            f"{connection.name}: profile {profile!r} ready (token {token_file}).", fg=typer.colors.GREEN
        )
    if not usable:
        typer.echo("Nothing to set up — no connection has an applicable grant.")


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
