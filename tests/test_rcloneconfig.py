"""Tests for the rclone remote writer and mount-command builder."""

from __future__ import annotations

import subprocess

import pytest

from cytario_cli.awsconfig import Connection
from cytario_cli.rcloneconfig import (
    RcloneError,
    remote_name,
    remote_options,
    remote_root,
    suggested_mount_command,
    write_remote,
)


def make_connection(**overrides) -> Connection:
    defaults = {
        "name": "My Bucket",
        "bucket_name": "data-bucket",
        "prefix": "",
        "region": "eu-central-1",
        "s3_endpoint": "https://s3.eu-central-1.amazonaws.com",
        "sts_endpoint": "https://sts.eu-central-1.amazonaws.com",
        "role_arn": "arn:aws:iam::123:role/cytario/provider-roles/ro",
        "access_level": "read-write",
    }
    defaults.update(overrides)
    return Connection(**defaults)


class TestRemoteOptions:
    def test_remote_name_is_namespaced(self):
        assert remote_name(make_connection(name="Lab Data 2024!")) == "cytario-lab-data-2024"

    def test_aws_s3_options(self):
        options = dict(remote_options(make_connection()))
        assert options == {
            "env_auth": "true",
            "profile": "cytario-my-bucket",
            "region": "eu-central-1",
            "provider": "AWS",
        }
        assert "endpoint" not in options  # AWS S3 needs no override

    def test_s3_compatible_options(self):
        options = dict(remote_options(make_connection(s3_endpoint="https://minio.internal:9000")))
        assert options["provider"] == "Other"
        assert options["endpoint"] == "https://minio.internal:9000"
        assert options["force_path_style"] == "true"

    def test_no_secret_values_stored(self):
        options = dict(remote_options(make_connection()))
        assert not any("key" in k or "secret" in k or "token" in k for k in options)


class TestWriteRemote:
    def test_runs_config_create_with_kv_pairs(self, monkeypatch):
        argv_seen = {}
        monkeypatch.setattr("cytario_cli.rcloneconfig.shutil.which", lambda _: "/usr/bin/rclone")

        def fake_run(argv, **kwargs):
            argv_seen["argv"] = argv
            argv_seen["kwargs"] = kwargs
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr("cytario_cli.rcloneconfig.subprocess.run", fake_run)
        remote = write_remote(make_connection())
        assert remote == "cytario-my-bucket"
        assert argv_seen["argv"] == [
            "rclone",
            "config",
            "create",
            "cytario-my-bucket",
            "s3",
            "env_auth=true",
            "profile=cytario-my-bucket",
            "region=eu-central-1",
            "provider=AWS",
        ]

    def test_missing_binary_is_actionable(self, monkeypatch):
        monkeypatch.setattr("cytario_cli.rcloneconfig.shutil.which", lambda _: None)
        with pytest.raises(RcloneError, match="install rclone"):
            write_remote(make_connection())

    def test_failure_raises_with_stderr(self, monkeypatch):
        monkeypatch.setattr("cytario_cli.rcloneconfig.shutil.which", lambda _: "/usr/bin/rclone")
        monkeypatch.setattr(
            "cytario_cli.rcloneconfig.subprocess.run",
            lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, "", "boom"),
        )
        with pytest.raises(RcloneError, match="boom"):
            write_remote(make_connection())

    def test_timeout_is_raised(self, monkeypatch):
        monkeypatch.setattr("cytario_cli.rcloneconfig.shutil.which", lambda _: "/usr/bin/rclone")

        def raise_timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 60)

        monkeypatch.setattr("cytario_cli.rcloneconfig.subprocess.run", raise_timeout)
        with pytest.raises(RcloneError, match="60s"):
            write_remote(make_connection())


class TestRemoteRoot:
    def test_root_without_prefix(self):
        assert remote_root(make_connection()) == "data-bucket"

    def test_root_strips_trailing_slash(self):
        assert remote_root(make_connection(prefix="studies/")) == "data-bucket/studies"


class TestSuggestedMountCommand:
    def _platform(self, monkeypatch, name):
        monkeypatch.setattr("cytario_cli.rcloneconfig.sys.platform", name)
        monkeypatch.setattr("sys.platform", name)

    def test_macos_prefers_nfsmount_with_cache_and_daemon_wait(self, monkeypatch):
        self._platform(monkeypatch, "darwin")
        command = suggested_mount_command(make_connection())
        assert command[:3] == ["rclone", "nfsmount", "cytario-my-bucket:data-bucket"]
        assert "~/mnt/my-bucket" in command
        assert "--vfs-cache-mode" in command
        assert "writes" in command
        assert "--daemon" in command
        assert "--daemon-wait" in command
        assert "--read-only" not in command

    def test_linux_uses_mount_with_daemon(self, monkeypatch):
        self._platform(monkeypatch, "linux")
        command = suggested_mount_command(make_connection())
        assert command[1] == "mount"
        assert "--daemon" in command
        assert "--daemon-wait" not in command

    def test_windows_mounts_drive_letter_foreground(self, monkeypatch):
        self._platform(monkeypatch, "win32")
        command = suggested_mount_command(make_connection())
        assert command[:3] == ["rclone", "mount", "cytario-my-bucket:data-bucket"]
        assert "X:" in command
        assert "--daemon" not in command
        assert "writes" in command  # write support still configured

    def test_read_only_connection_gets_read_only_flag(self, monkeypatch):
        self._platform(monkeypatch, "linux")
        command = suggested_mount_command(make_connection(access_level="read-only"))
        assert "--read-only" in command
        assert "writes" not in command

    def test_read_only_on_windows_gets_read_only_flag(self, monkeypatch):
        self._platform(monkeypatch, "win32")
        command = suggested_mount_command(make_connection(access_level="read-only"))
        assert "--read-only" in command
        assert "--vfs-cache-mode" not in command
