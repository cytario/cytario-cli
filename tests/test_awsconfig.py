"""Tests for the AWS profile writer and token files."""

from __future__ import annotations

import configparser
import re
from pathlib import Path

import pytest

from cytario_cli.awsconfig import Connection, profile_name, write_profile
from cytario_cli.config import write_token_file


def count_managed(raw: str) -> int:
    return len(re.findall(r"^\[profile cytario-", raw, re.MULTILINE))


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
    def test_writes_managed_profile_without_global_header(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cytario_cli.awsconfig.AWS_CONFIG_PATH", tmp_path / "aws" / "config")
        token_file = write_token_file("my-bucket", "token-123")
        connection = make_connection(name="My Bucket")

        profile = write_profile(connection, token_file)

        raw = (tmp_path / "aws" / "config").read_text()
        assert raw.count("managed by cytario-cli") == 1  # comment travels with the section
        # and it sits directly above the section it annotates, never as a stray banner
        assert "# managed by cytario-cli\n[profile cytario-my-bucket]" in raw

        parser = configparser.RawConfigParser()
        parser.read(tmp_path / "aws" / "config")
        section = parser[f"profile {profile}"]
        assert section["role_arn"] == connection.role_arn
        assert section["web_identity_token_file"] == str(token_file)
        assert section["region"] == "eu-central-1"
        assert "endpoint_url" not in section  # AWS S3 needs no override

    def test_preserves_foreign_profiles_and_comments(self, tmp_path, monkeypatch):
        config_path = tmp_path / "aws" / "config"
        monkeypatch.setattr("cytario_cli.awsconfig.AWS_CONFIG_PATH", config_path)
        config_path.parent.mkdir(parents=True)
        user_content = "# my personal note\n[profile personal]\nregion = us-west-2\n\n"
        config_path.write_text(user_content)

        write_profile(make_connection(name="My Bucket"), Path("/tmp/t"))

        raw = config_path.read_text()
        assert raw.startswith(user_content)  # user content untouched, in order
        assert "[profile personal]" in raw
        assert "[profile cytario-my-bucket]" in raw

    def test_readds_profile_after_manual_removal(self, tmp_path, monkeypatch):
        """Deleting the managed block from .aws/config must not break setup."""
        config_path = tmp_path / "aws" / "config"
        monkeypatch.setattr("cytario_cli.awsconfig.AWS_CONFIG_PATH", config_path)

        write_profile(make_connection(), Path("/tmp/a"))
        assert "[profile cytario-my-bucket]" in config_path.read_text()

        # the user deletes the block (comment + section) by hand
        config_path.write_text("[profile personal]\nregion = us-west-2\n")

        profile = write_profile(make_connection(), Path("/tmp/b"))

        raw = config_path.read_text()
        assert "[profile cytario-my-bucket]" in raw
        assert "[profile personal]" in raw
        parser = configparser.RawConfigParser()
        parser.read(config_path)
        assert parser[f"profile {profile}"]["web_identity_token_file"] == "/tmp/b"

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

        raw = (tmp_path / "aws" / "config").read_text()
        assert raw.count("[profile cytario-my-bucket]") == 1  # replaced in place, not duplicated

        parser = configparser.RawConfigParser()
        parser.read(tmp_path / "aws" / "config")
        section = parser["profile cytario-my-bucket"]
        assert section["role_arn"] == "arn:aws:iam::123:role/new"
        assert section["web_identity_token_file"] == "/tmp/b"
        assert len(parser.sections()) == 1

    def test_upsert_first_of_many_leaves_following_blocks_intact(self, tmp_path, monkeypatch):
        """Regression: refreshing a non-final profile must not eat what follows."""
        config_path = tmp_path / "aws" / "config"
        monkeypatch.setattr("cytario_cli.awsconfig.AWS_CONFIG_PATH", config_path)
        write_profile(make_connection(), Path("/tmp/a"))
        write_profile(make_connection(name="Other"), Path("/tmp/b"))
        write_profile(make_connection(name="Third"), Path("/tmp/c"))

        write_profile(make_connection(role_arn="arn:aws:iam::123:role/refreshed"), Path("/tmp/a2"))

        raw = config_path.read_text()
        assert raw.count("[profile cytario-my-bucket]") == 1
        assert "[profile cytario-other]" in raw
        assert "[profile cytario-third]" in raw
        assert "# managed by cytario-cli\n[profile cytario-other]" in raw  # comment survived
        parser = configparser.RawConfigParser()
        parser.read(config_path)
        assert parser["profile cytario-other"]["web_identity_token_file"] == "/tmp/b"
        assert parser["profile cytario-my-bucket"]["role_arn"] == "arn:aws:iam::123:role/refreshed"

    def test_setup_all_twice_yields_exactly_n_sections(self, tmp_path, monkeypatch):
        config_path = tmp_path / "aws" / "config"
        monkeypatch.setattr("cytario_cli.awsconfig.AWS_CONFIG_PATH", config_path)
        connections = [make_connection(), make_connection(name="Other"), make_connection(name="Third")]

        for _ in range(2):
            for connection in connections:
                write_profile(connection, Path(f"/tmp/{connection.slug}"))

        parser = configparser.RawConfigParser(strict=True)
        parser.read(config_path)
        assert len(parser.sections()) == 3
        assert count_managed(config_path.read_text()) == 3  # no duplicates at text level either

    def test_heals_fused_value_and_duplicate_sections(self, tmp_path, monkeypatch):
        """Regression for configs mangled by the pre-ConfigUpdater writer."""
        config_path = tmp_path / "aws" / "config"
        monkeypatch.setattr("cytario_cli.awsconfig.AWS_CONFIG_PATH", config_path)
        damaged = (
            "# managed by cytario-cli\n"
            "[profile cytario-my-bucket]\n"
            "region = eu-central-1\n"
            "role_arn = arn:aws:iam::123:role/cytario/storage-ro\n"
            "web_identity_token_file = /Users/someone/.aws/cytario/my-bucket/id_token"
            "[profile cytario-other]\n"
            "region = eu-central-1\n"
            "role_arn = arn:aws:iam::123:role/cytario/storage-other\n"
            "web_identity_token_file = /Users/someone/.aws/cytario/other/id_token\n"
            "# managed by cytario-cli\n"
            "[profile cytario-other]\n"
            "region = eu-central-1\n"
            "role_arn = arn:aws:iam::123:role/cytario/storage-dup\n"
            "web_identity_token_file = /Users/someone/.aws/cytario/other/id_token\n"
            "# managed by cytario-cli\n"
            "[profile personal]\n"
            "region = us-west-2\n"
        )
        config_path.parent.mkdir(parents=True)
        config_path.write_text(damaged)

        write_profile(make_connection(), Path("/tmp/t"))

        raw = config_path.read_text()
        assert raw.count("[profile cytario-my-bucket]") == 1
        assert raw.count("[profile cytario-other]") == 1  # duplicate dropped, fused header restored
        assert "id_token[profile" not in raw  # fusion gone
        assert "[profile personal]" in raw
        parser = configparser.RawConfigParser(strict=True)
        parser.read(config_path)
        assert parser["profile cytario-my-bucket"]["web_identity_token_file"] == "/tmp/t"
        assert (
            parser["profile cytario-other"]["web_identity_token_file"]
            == "/Users/someone/.aws/cytario/other/id_token"
        )

    def test_validates_before_writing(self, tmp_path, monkeypatch):
        """A mangled write must raise, not corrupt the file."""
        config_path = tmp_path / "aws" / "config"
        monkeypatch.setattr("cytario_cli.awsconfig.AWS_CONFIG_PATH", config_path)
        before = "[profile personal]\nregion = us-west-2\n"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(before)

        from cytario_cli import awsconfig

        def broken_upsert(updater, connection, token_file):
            updater["profile personal"]["region"] = "x[profile oops]"

        monkeypatch.setattr(awsconfig, "_upsert", broken_upsert)
        with pytest.raises(ValueError, match="fused value"):
            write_profile(make_connection(), Path("/tmp/t"))
        assert config_path.read_text() == before  # untouched on failure


class TestTokenFile:
    def test_write_token_file_creates_0600_file(self, tmp_path, monkeypatch):
        from cytario_cli import config

        monkeypatch.setattr(config, "TOKENS_DIR", tmp_path / "tokens")
        path = write_token_file("conn-a", "the-token")
        assert path == tmp_path / "tokens" / "conn-a" / "id_token"
        assert path.read_text() == "the-token"
        assert path.stat().st_mode & 0o777 == 0o600
