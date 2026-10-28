"""Tests for `cytario rclone setup` (remote writing, output, error paths)."""

from __future__ import annotations

import base64
import json
import subprocess
import time

import pytest
import respx
import typer

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


class TestRcloneSetup:
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

    def stub_rclone(self, monkeypatch, tmp_path):
        """Stub rclone: record config-create argv, succeed config file."""
        from cytario_cli import rcloneconfig

        seen = {"argv": []}
        monkeypatch.setattr(rcloneconfig.shutil, "which", lambda _: "/usr/bin/rclone")

        def fake_run(argv, **kwargs):
            if argv[:3] == ["rclone", "config", "file"]:
                return subprocess.CompletedProcess(argv, 0, str(tmp_path / "rclone.conf"), "")
            seen["argv"].append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(rcloneconfig.subprocess, "run", fake_run)
        return seen

    @respx.mock
    def test_setup_all_writes_profile_token_and_remote(self, tmp_path, monkeypatch, capsys):
        state = self.make_state(tmp_path, monkeypatch)
        state.save()
        seen = self.stub_rclone(monkeypatch, tmp_path)
        respx.get(f"{HOST}/api/me/connections").respond(json=connection_payload())

        cli.rclone_setup(setup_all=True, name=None, host=None)

        output = capsys.readouterr().out
        assert "Demo: rclone remote 'cytario-demo' ready via profile 'cytario-demo'" in output
        assert "rclone config create" not in output  # no argv noise
        assert seen["argv"] == [
            [
                "rclone",
                "config",
                "create",
                "cytario-demo",
                "s3",
                "env_auth=true",
                "profile=cytario-demo",
                "region=eu-central-1",
                "provider=AWS",
            ]
        ]
        # the AWS profile the remote federates through was refreshed too
        aws_config = (tmp_path / "aws" / "config").read_text()
        assert "[profile cytario-demo]" in aws_config
        assert "web_identity_token_file" in aws_config
        assert (tmp_path / "tokens" / "demo" / "id_token").read_text() == state.id_token
        # the platform-correct mount command is suggested
        assert "mount: rclone" in output
        assert "cytario-demo:demo-bucket/studies" in output

    @respx.mock
    def test_no_usable_connection_exits_1(self, tmp_path, monkeypatch, capsys):
        self.make_state(tmp_path, monkeypatch).save()
        self.stub_rclone(monkeypatch, tmp_path)
        payload = connection_payload()
        payload["connections"][0]["roleArn"] = None
        payload["connections"][0]["accessLevel"] = None
        respx.get(f"{HOST}/api/me/connections").respond(json=payload)

        with pytest.raises(typer.Exit) as exit_info:
            cli.rclone_setup(setup_all=True, name=None, host=None)
        assert exit_info.value.exit_code == 1
        assert "none has a grant applicable" in capsys.readouterr().out

    @respx.mock
    def test_rustfs_grant_is_usable_and_gets_placeholder_arn(self, tmp_path, monkeypatch, capsys):
        """An ARN-less RustFS grant (access level only) sets up fully."""
        state = self.make_state(tmp_path, monkeypatch)
        state.save()
        seen = self.stub_rclone(monkeypatch, tmp_path)
        payload = connection_payload()
        payload["connections"][0].update(
            {
                "roleArn": None,
                "accessLevel": "admin",
                "s3Endpoint": "https://rustfs.internal:9000",
                "stsEndpoint": "https://rustfs.internal:9000",
            }
        )
        respx.get(f"{HOST}/api/me/connections").respond(json=payload)

        cli.rclone_setup(setup_all=True, name=None, host=None)

        output = capsys.readouterr().out
        assert "Demo: rclone remote 'cytario-demo' ready via profile 'cytario-demo'" in output
        assert seen["argv"] == [
            [
                "rclone",
                "config",
                "create",
                "cytario-demo",
                "s3",
                "env_auth=true",
                "profile=cytario-demo",
                "region=eu-central-1",
                "provider=Other",
                "endpoint=https://rustfs.internal:9000",
                "force_path_style=true",
            ]
        ]
        aws_config = (tmp_path / "aws" / "config").read_text()
        assert "role_arn = arn:aws:iam:::role/cytario-rustfs" in aws_config
        assert "endpoint_url = https://rustfs.internal:9000" in aws_config
        assert (tmp_path / "tokens" / "demo" / "id_token").read_text() == state.id_token

    @respx.mock
    def test_public_connection_gets_an_anonymous_remote(self, tmp_path, monkeypatch, capsys):
        """A public (credential-less) connection writes an anonymous remote —
        no token file, no AWS profile, rclone's blank-credential shape."""
        state = self.make_state(tmp_path, monkeypatch)
        state.save()
        seen = self.stub_rclone(monkeypatch, tmp_path)
        payload = connection_payload()
        payload["connections"][0].update({"roleArn": None, "accessLevel": "read-only"})
        respx.get(f"{HOST}/api/me/connections").respond(json=payload)

        cli.rclone_setup(setup_all=True, name=None, host=None)

        output = capsys.readouterr().out
        assert "Demo: rclone remote 'cytario-demo' ready (anonymous — public connection)" in output
        assert seen["argv"] == [
            [
                "rclone",
                "config",
                "create",
                "cytario-demo",
                "s3",
                "provider=AWS",
            ]
        ]
        # nothing to federate: no AWS profile and no token file are written
        assert not (tmp_path / "aws" / "config").exists()
        assert not (tmp_path / "tokens" / "demo").exists()

    @respx.mock
    def test_public_connection_on_s3_compatible_backend_takes_the_rustfs_path(
        self, tmp_path, monkeypatch, capsys
    ):
        """A null-ARN read-only grant on a non-AWS endpoint is indistinguishable
        from a RustFS grant — the wire shapes are identical — so it keeps the
        working RustFS path (placeholder-ARN profile + federating remote)
        rather than degrading to anonymous."""
        state = self.make_state(tmp_path, monkeypatch)
        state.save()
        seen = self.stub_rclone(monkeypatch, tmp_path)
        payload = connection_payload()
        payload["connections"][0].update(
            {
                "roleArn": None,
                "accessLevel": "read-only",
                "s3Endpoint": "https://mirror.example.org",
                "stsEndpoint": "https://mirror.example.org",
            }
        )
        respx.get(f"{HOST}/api/me/connections").respond(json=payload)

        cli.rclone_setup(setup_all=True, name=None, host=None)

        output = capsys.readouterr().out
        assert "Demo: rclone remote 'cytario-demo' ready via profile 'cytario-demo'" in output
        assert seen["argv"] == [
            [
                "rclone",
                "config",
                "create",
                "cytario-demo",
                "s3",
                "env_auth=true",
                "profile=cytario-demo",
                "region=eu-central-1",
                "provider=Other",
                "endpoint=https://mirror.example.org",
                "force_path_style=true",
            ]
        ]
        aws_config = (tmp_path / "aws" / "config").read_text()
        assert "role_arn = arn:aws:iam:::role/cytario-rustfs" in aws_config

    @respx.mock
    def test_mixed_setup_all_writes_both_shapes(self, tmp_path, monkeypatch, capsys):
        """--all over a public + a private connection writes each its own shape."""
        state = self.make_state(tmp_path, monkeypatch)
        state.save()
        seen = self.stub_rclone(monkeypatch, tmp_path)
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

        cli.rclone_setup(setup_all=True, name=None, host=None)

        output = capsys.readouterr().out
        assert "Demo: rclone remote 'cytario-demo' ready via profile 'cytario-demo'" in output
        assert (
            "Public Data: rclone remote 'cytario-public-data' ready (anonymous — public connection)" in output
        )
        create_args = [argv for argv in seen["argv"] if argv[:3] == ["rclone", "config", "create"]]
        assert [argv[3] for argv in create_args] == ["cytario-demo", "cytario-public-data"]
        aws_config = (tmp_path / "aws" / "config").read_text()
        assert "[profile cytario-demo]" in aws_config
        assert "[profile cytario-public-data]" not in aws_config

    @respx.mock
    def test_unknown_name_exits_1(self, tmp_path, monkeypatch, capsys):
        self.make_state(tmp_path, monkeypatch).save()
        self.stub_rclone(monkeypatch, tmp_path)
        respx.get(f"{HOST}/api/me/connections").respond(json=connection_payload())

        with pytest.raises(typer.Exit) as exit_info:
            cli.rclone_setup(setup_all=False, name="Nope", host=None)
        assert exit_info.value.exit_code == 1
        assert "No connection named 'Nope'" in capsys.readouterr().out

    @respx.mock
    def test_no_selection_exits_2(self, tmp_path, monkeypatch, capsys):
        self.make_state(tmp_path, monkeypatch).save()
        respx.get(f"{HOST}/api/me/connections").respond(json=connection_payload())
        with pytest.raises(typer.Exit) as exit_info:
            cli.rclone_setup(setup_all=False, name=None, host=None)
        assert exit_info.value.exit_code == 2
        assert "use --all" in capsys.readouterr().out

    @respx.mock
    def test_rclone_failure_exits_1_with_actionable_message(self, tmp_path, monkeypatch, capsys):
        from cytario_cli import rcloneconfig

        self.make_state(tmp_path, monkeypatch).save()
        monkeypatch.setattr(rcloneconfig.shutil, "which", lambda _: None)
        respx.get(f"{HOST}/api/me/connections").respond(json=connection_payload())

        with pytest.raises(typer.Exit) as exit_info:
            cli.rclone_setup(setup_all=True, name=None, host=None)
        assert exit_info.value.exit_code == 1
        output = capsys.readouterr().out
        assert "could not write rclone remote" in output
        assert "install rclone" in output
