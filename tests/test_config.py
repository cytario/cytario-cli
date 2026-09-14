"""Tests for the persisted CLI state."""

from __future__ import annotations

from cytario_cli.config import CliState


class TestCliState:
    def test_round_trip(self, tmp_path, monkeypatch):
        from cytario_cli import config

        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
        state = CliState(
            host="https://app.example.com",
            issuer="https://auth.example.com/realms/cytario",
            authorization_endpoint="https://auth.example.com/auth",
            token_endpoint="https://auth.example.com/token",
            refresh_token="grant-1",
            id_token="tok",
            id_token_expires_at=123.0,
        )
        state.save()

        loaded = CliState.load()
        assert loaded == state

    def test_load_returns_none_when_absent(self, tmp_path, monkeypatch):
        from cytario_cli import config

        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "missing.json")
        assert CliState.load() is None

    def test_load_returns_none_when_truncated(self, tmp_path, monkeypatch):
        from cytario_cli import config

        path = tmp_path / "state.json"
        path.write_text('{"host": "https://x"}')  # missing required keys
        monkeypatch.setattr(config, "STATE_FILE", path)
        assert CliState.load() is None
