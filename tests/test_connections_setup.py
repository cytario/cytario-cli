"""Tests for `cytario connections setup` (profile writing, output, gates)."""

from __future__ import annotations

import base64
import json
import time

import respx

from cytario_cli import cli
from cytario_cli.config import CliState

HOST = "https://app.example.com"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_id_token() -> str:
    header = _b64url(b'{"alg":"RS256"}')
    claims = {"sub": "e0e28a62", "email": "user@example.com", "exp": int(time.time()) + 3600}
    payload = _b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.{_b64url(b'signature')}"


def connection_payload() -> dict:
    return {
        "connections": [
            {
                "id": "c1",
                "name": "Demo",
                "bucketName": "demo-bucket",
                "prefix": "studies/",
                "region": "eu-central-1",
                "s3Endpoint": "https://s3.eu-central-1.amazonaws.com",
                "stsEndpoint": "https://sts.eu-central-1.amazonaws.com",
                "roleArn": "arn:aws:iam::123:role/cytario/provider-roles/rw",
                "accessLevel": "read-write",
            }
        ]
    }


class TestConnectionsSetup:
    def make_state(self, tmp_path, monkeypatch):
        from cytario_cli import config

        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
        monkeypatch.setattr(config, "TOKENS_DIR", tmp_path / "tokens")
        from cytario_cli import awsconfig

        monkeypatch.setattr(awsconfig, "AWS_CONFIG_PATH", tmp_path / "aws" / "config")
        return CliState(
            host=HOST,
            issuer="https://auth.example.com/realms/cytario",
            authorization_endpoint="https://auth.example.com/auth",
            token_endpoint="https://auth.example.com/token",
            refresh_token="grant",
            id_token=make_id_token(),
            id_token_expires_at=time.time() + 3600,
            access_token="access",
            access_token_expires_at=time.time() + 3600,
        )

    @respx.mock
    def test_setup_all_writes_profiles_and_tokens(self, tmp_path, monkeypatch, capsys):
        state = self.make_state(tmp_path, monkeypatch)
        state.save()
        respx.get(f"{HOST}/api/me/connections").respond(json=connection_payload())

        cli.connections_setup(setup_all=True, name=None, host=None)

        output = capsys.readouterr().out
        assert "Demo: profile 'cytario-demo' ready" in output
        aws_config = (tmp_path / "aws" / "config").read_text()
        assert "[profile cytario-demo]" in aws_config
        assert (tmp_path / "tokens" / "demo" / "id_token").read_text() == state.id_token

    @respx.mock
    def test_public_connection_is_skipped_with_a_note(self, tmp_path, monkeypatch, capsys):
        """A public (credential-less) connection gets no profile — no config
        can express anonymous reads — and the output points at rclone."""
        state = self.make_state(tmp_path, monkeypatch)
        state.save()
        payload = connection_payload()
        payload["connections"][0].update(
            {
                "name": "Public Data",
                "roleArn": None,
                "accessLevel": "read-only",
            }
        )
        respx.get(f"{HOST}/api/me/connections").respond(json=payload)

        cli.connections_setup(setup_all=True, name=None, host=None)

        output = capsys.readouterr().out
        assert "Public (credential-less) connections are skipped here" in output
        assert "rclone setup" in output
        assert "No connection had an AWS profile to write" in output
        assert not (tmp_path / "aws" / "config").exists()
        assert not (tmp_path / "tokens").exists()

    @respx.mock
    def test_mixed_selection_sets_up_the_private_one(self, tmp_path, monkeypatch, capsys):
        state = self.make_state(tmp_path, monkeypatch)
        state.save()
        payload = connection_payload()
        payload["connections"].append(
            {
                "id": "c2",
                "name": "Public Data",
                "bucketName": "open-bucket",
                "prefix": "",
                "region": "us-east-1",
                "s3Endpoint": "https://s3.us-east-1.amazonaws.com",
                "stsEndpoint": "https://sts.us-east-1.amazonaws.com",
                "roleArn": None,
                "accessLevel": "read-only",
            }
        )
        respx.get(f"{HOST}/api/me/connections").respond(json=payload)

        cli.connections_setup(setup_all=True, name=None, host=None)

        output = capsys.readouterr().out
        assert "Public (credential-less) connections are skipped here" in output
        assert "Demo: profile 'cytario-demo' ready" in output
        aws_config = (tmp_path / "aws" / "config").read_text()
        assert "[profile cytario-demo]" in aws_config
        assert "[profile cytario-public-data]" not in aws_config


class TestConnectionsList:
    def make_state(self, tmp_path, monkeypatch):
        from cytario_cli import config

        monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
        return CliState(
            host=HOST,
            issuer="https://auth.example.com/realms/cytario",
            authorization_endpoint="https://auth.example.com/auth",
            token_endpoint="https://auth.example.com/token",
            refresh_token="grant",
            id_token=make_id_token(),
            id_token_expires_at=time.time() + 3600,
            access_token="access",
            access_token_expires_at=time.time() + 3600,
        )

    @respx.mock
    def test_public_connection_is_labeled(self, tmp_path, monkeypatch, capsys):
        self.make_state(tmp_path, monkeypatch).save()
        payload = connection_payload()
        payload["connections"][0].update({"roleArn": None, "accessLevel": "read-only"})
        respx.get(f"{HOST}/api/me/connections").respond(json=payload)

        cli.connections_list(host=None, as_json=False)

        output = capsys.readouterr().out
        assert "Demo" in output
        assert "public — anonymous reads" in output

    @respx.mock
    def test_public_connection_json_carries_null_arn(self, tmp_path, monkeypatch, capsys):
        self.make_state(tmp_path, monkeypatch).save()
        payload = connection_payload()
        payload["connections"][0].update({"roleArn": None, "accessLevel": "read-only"})
        respx.get(f"{HOST}/api/me/connections").respond(json=payload)

        cli.connections_list(host=None, as_json=True)

        rows = json.loads(capsys.readouterr().out)
        assert rows[0]["roleArn"] is None
        assert rows[0]["accessLevel"] == "read-only"
