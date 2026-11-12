"""Tests for token reuse and refresh serialisation (C-625)."""

from __future__ import annotations

import base64
import json
import threading
import time

import pytest
import respx

from cytario_cli import cli, config
from cytario_cli.config import CliState, refresh_lock


def make_id_token(expiry: float, sub: str = "user-1") -> str:
    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    header = b64url(b'{"alg":"RS256"}')
    payload = b64url(f'{{"sub":"{sub}","exp":{expiry}}}'.encode())
    return f"{header}.{payload}.{b64url(b'signature')}"


@pytest.fixture(name="state_file")
def _state_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(config, "STATE_FILE", path)
    return path


def make_state(expiry: float, refresh_token: str = "grant-1", expires_in: float = 300) -> CliState:
    now = time.time()
    return CliState(
        host="https://app.example.com",
        issuer="https://auth.example.com/realms/cytario",
        authorization_endpoint="https://auth.example.com/auth",
        token_endpoint="https://auth.example.com/token",
        refresh_token=refresh_token,
        id_token=make_id_token(expiry),
        id_token_expires_at=expiry,
        access_token="access-1",
        access_token_expires_at=now + expires_in,
    )


TOKEN_RESPONSE = {
    "refresh_token": "grant-2",
    "id_token": make_id_token(time.time() + 300),
    "access_token": "access-2",
    "expires_in": 300,
}


class TestFreshTokenIsReused:
    @respx.mock
    def test_newly_minted_token_is_not_refreshed(self, state_file):
        """A token minted with the realm's short TTL still counts as fresh."""
        refresh_route = respx.post("https://auth.example.com/token").respond(json=TOKEN_RESPONSE)
        state = make_state(expiry=time.time() + 290)  # what a just-issued token looks like
        state.save()

        assert cli._fresh_id_token(state) == state.id_token
        assert not refresh_route.called

    @respx.mock
    def test_repeated_commands_refresh_once(self, state_file):
        refresh_route = respx.post("https://auth.example.com/token").respond(json=TOKEN_RESPONSE)
        state = make_state(expiry=time.time() + 10)
        state.save()

        first = cli._fresh_id_token(state)
        second = cli._fresh_id_token(CliState.load())

        assert first == second == TOKEN_RESPONSE["id_token"]
        assert refresh_route.call_count == 1

    @respx.mock
    def test_access_and_id_token_refresh_in_one_grant(self, state_file):
        refresh_route = respx.post("https://auth.example.com/token").respond(json=TOKEN_RESPONSE)
        state = make_state(expiry=time.time() + 290, expires_in=10)
        state.save()

        access = cli._fresh_access_token(state)
        id_token = cli._fresh_id_token(state)

        assert access == "access-2"
        assert id_token == TOKEN_RESPONSE["id_token"]
        assert refresh_route.call_count == 1


class TestRotationRace:
    @respx.mock
    def test_rotated_grant_is_adopted_not_a_sign_out(self, state_file):
        """Another invocation already redeemed the grant and persisted the successor."""
        respx.post("https://auth.example.com/token").respond(status_code=400, json={"error": "invalid_grant"})
        stale = make_state(expiry=time.time() + 10, refresh_token="grant-1")
        successor = make_state(expiry=time.time() + 300, refresh_token="grant-2")
        successor.id_token = make_id_token(time.time() + 300)
        successor.id_token_expires_at = time.time() + 300
        successor.save()

        assert cli._fresh_id_token(stale) == successor.id_token
        assert stale.refresh_token == "grant-2"
        assert state_file.exists()

    @respx.mock
    def test_revoked_grant_still_signs_out(self, state_file):
        respx.post("https://auth.example.com/token").respond(status_code=400, json={"error": "invalid_grant"})
        state = make_state(expiry=time.time() + 10)
        state.save()

        with pytest.raises(cli.typer.Exit) as exit_info:
            cli._fresh_id_token(state)

        assert exit_info.value.exit_code == 2
        assert not state_file.exists()

    @respx.mock
    def test_transient_failure_keeps_the_session(self, state_file):
        respx.post("https://auth.example.com/token").respond(status_code=503, text="unavailable")
        state = make_state(expiry=time.time() + 10)
        state.save()

        with pytest.raises(cli.typer.Exit) as exit_info:
            cli._fresh_id_token(state)

        assert exit_info.value.exit_code == 1
        assert CliState.load().refresh_token == "grant-1"


class TestRefreshLock:
    def test_concurrent_refreshes_are_serialised(self, state_file):
        order: list[str] = []

        def worker(name: str) -> None:
            with refresh_lock():
                order.append(f"{name}-enter")
                time.sleep(0.1)
                order.append(f"{name}-exit")

        threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert order in (
            ["a-enter", "a-exit", "b-enter", "b-exit"],
            ["b-enter", "b-exit", "a-enter", "a-exit"],
        )

    @respx.mock
    def test_lock_holder_rotating_first_leaves_the_second_served(self, state_file):
        """The waiter re-reads the state under the lock and reuses the new token."""
        state_file.write_text(
            json.dumps(
                make_state(expiry=time.time() + 10, refresh_token="grant-1").__dict__,
            )
        )
        stale = CliState.load()
        respx.post("https://auth.example.com/token").respond(json=TOKEN_RESPONSE)

        with refresh_lock():
            rotated = make_state(expiry=time.time() + 300, refresh_token="grant-2")
            rotated.id_token = TOKEN_RESPONSE["id_token"]
            rotated.id_token_expires_at = time.time() + 300
            rotated.save()

        assert cli._fresh_id_token(stale) == TOKEN_RESPONSE["id_token"]
