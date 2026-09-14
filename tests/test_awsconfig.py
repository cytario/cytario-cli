"""Tests for the AWS profile writer and token files."""

from __future__ import annotations

import configparser
from pathlib import Path

from cytario_cli.awsconfig import Connection, profile_name, write_profile
from cytario_cli.config import write_token_file


def make_connection(**overrides) -> Connection:
    defaults = {
        "name": "My Bucket",
        "bucket_name": "data-bucket",
        "prefix": "",
        "region": "eu-central-1",
        "s3_endpoint": "https://s3.eu-central-1.amazonaws.com",
        "sts_endpoint": "https://sts.eu-central-1.amazonaws.com",
        "role_arn": "arn:aws:iam::123:role/cytario/provider-roles/ro",
        "access_level": "read-only",
    }
    defaults.update(overrides)
    return Connection(**defaults)


class TestConnection:
    def test_from_api_payload(self):
        connection = Connection.from_api(
            {
                "name": "Demo",
                "bucketName": "demo-bucket",
                "prefix": "studies/",
                "region": "us-east-1",
                "s3Endpoint": "https://s3.amazonaws.com",
                "stsEndpoint": "https://sts.amazonaws.com",
                "roleArn": "arn:aws:iam::1:role/x",
                "accessLevel": "admin",
            }
        )
        assert connection.slug == "demo"
        assert connection.role_arn == "arn:aws:iam::1:role/x"

    def test_slug_sanitizes_names(self):
        assert make_connection(name="Lab Data 2024!").slug == "lab-data-2024"
        assert profile_name(make_connection(name="Lab Data 2024!")) == "cytario-lab-data-2024"


class TestWriteProfile:
    def test_writes_managed_profile(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cytario_cli.awsconfig.AWS_CONFIG_PATH", tmp_path / "aws" / "config")
        token_file = write_token_file("my-bucket", "token-123")
        connection = make_connection(name="My Bucket")

        profile = write_profile(connection, token_file)

        parser = configparser.RawConfigParser()
        parser.read(tmp_path / "aws" / "config")
        section = parser[f"profile {profile}"]
        assert section["role_arn"] == connection.role_arn
        assert section["web_identity_token_file"] == str(token_file)
        assert section["region"] == "eu-central-1"
        assert "endpoint_url" not in section  # AWS S3 needs no override

    def test_s3_compatible_gets_endpoint_url(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cytario_cli.awsconfig.AWS_CONFIG_PATH", tmp_path / "aws" / "config")
        connection = make_connection(s3_endpoint="https://minio.internal:9000")
        token_file = Path("/tmp/t")

        profile = write_profile(connection, token_file)

        parser = configparser.RawConfigParser()
        parser.read(tmp_path / "aws" / "config")
        assert parser[f"profile {profile}"]["endpoint_url"] == "https://minio.internal:9000"

    def test_upsert_replaces_only_our_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cytario_cli.awsconfig.AWS_CONFIG_PATH", tmp_path / "aws" / "config")
        write_profile(make_connection(), Path("/tmp/a"))
        write_profile(make_connection(role_arn="arn:aws:iam::123:role/new"), Path("/tmp/b"))

        parser = configparser.RawConfigParser()
        parser.read(tmp_path / "aws" / "config")
        section = parser["profile cytario-my-bucket"]
        assert section["role_arn"] == "arn:aws:iam::123:role/new"
        assert section["web_identity_token_file"] == "/tmp/b"
        assert len(parser.sections()) == 1


class TestTokenFile:
    def test_write_token_file_creates_0600_file(self, tmp_path, monkeypatch):
        from cytario_cli import config

        monkeypatch.setattr(config, "TOKENS_DIR", tmp_path / "tokens")
        path = write_token_file("conn-a", "the-token")
        assert path == tmp_path / "tokens" / "conn-a" / "id_token"
        assert path.read_text() == "the-token"
        assert path.stat().st_mode & 0o777 == 0o600
