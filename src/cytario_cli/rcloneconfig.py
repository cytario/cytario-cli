"""Rclone remote management.

Writes one named rclone remote per connection (``[cytario-<slug>]``) into the
rclone config the rclone binary itself resolves (``rclone config create``) so
the connection can be mounted locally with ``rclone mount``/``nfsmount``. The
remote stores no credentials: ``env_auth = true`` + ``profile = cytario-<slug>``
routes authentication through the AWS CLI profile ``connections setup`` wrote
(web_identity_token_file), so rclone federates per operation exactly like the
AWS tooling. Remotes are upserted through rclone itself, which preserves every
other remote in the file byte-for-byte; the prefixed name keeps user-managed
remotes safe.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from .awsconfig import Connection, profile_name

REMOTE_PREFIX = "cytario-"
READ_ONLY_ACCESS_LEVEL = "read-only"
INSTALL_HINT = "install rclone first: https://rclone.org/install/"


class RcloneError(Exception):
    """Raised when rclone is missing or its config command fails."""


def remote_name(connection: Connection) -> str:
    """Return the rclone remote name for a connection."""
    return f"{REMOTE_PREFIX}{connection.slug}"


def _is_aws_s3(connection: Connection) -> bool:
    return "amazonaws.com" in connection.s3_endpoint


def remote_options(connection: Connection) -> list[tuple[str, str]]:
    """Return the rclone s3 key/value pairs for a connection's remote.

    The backend type itself is a positional argument to `rclone config
    create` and is deliberately not among the pairs.
    """
    options: list[tuple[str, str]] = [
        ("env_auth", "true"),
        ("profile", profile_name(connection)),
        ("region", connection.region),
    ]
    if _is_aws_s3(connection):
        options.append(("provider", "AWS"))
    else:
        options.extend(
            [
                ("provider", "Other"),
                ("endpoint", connection.s3_endpoint),
                ("force_path_style", "true"),
            ]
        )
    return options


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603
            argv, check=False, capture_output=True, text=True, timeout=60
        )
    except FileNotFoundError as error:
        raise RcloneError(f"rclone was not found on PATH — {INSTALL_HINT}") from error
    except subprocess.TimeoutExpired as error:
        raise RcloneError("rclone config create did not finish within 60s.") from error


def write_remote(connection: Connection) -> str:
    """Upsert the rclone remote for a connection; return the remote name.

    Delegates to ``rclone config create <name> s3 k=v ...`` (non-interactive,
    replaces only the named section) so the config file's location, format,
    and any user-managed remotes stay rclone's concern.
    """
    if shutil.which("rclone") is None:
        raise RcloneError(f"rclone was not found on PATH — {INSTALL_HINT}")
    argv = [
        "rclone",
        "config",
        "create",
        remote_name(connection),
        "s3",
        *[f"{key}={value}" for key, value in remote_options(connection)],
    ]
    result = _run(argv)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RcloneError(f"rclone config create failed: {message}")
    return remote_name(connection)


def rclone_config_path() -> str | None:
    """Return the config file path rclone itself resolves, for display."""
    if shutil.which("rclone") is None:
        return None
    result = _run(["rclone", "config", "file"])
    if result.returncode != 0:
        return None
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None


def remote_root(connection: Connection) -> str:
    """Return the remote path (bucket/prefix) to mount."""
    prefix = connection.prefix.rstrip("/")
    return f"{connection.bucket_name}/{prefix}" if prefix else connection.bucket_name


def suggested_mount_command(connection: Connection) -> list[str]:
    """Return a ready-to-run mount command for the detected platform."""
    remote = remote_name(connection)
    root = remote_root(connection)
    profile = profile_name(connection)
    read_only = connection.access_level == READ_ONLY_ACCESS_LEVEL
    windows = sys.platform == "win32"

    if windows:
        command = ["rclone", "mount", f"{remote}:{root}", "X:"]
        if read_only:
            command.append("--read-only")
        else:
            command.extend(["--vfs-cache-mode", "writes"])
        return command

    subcommand = "nfsmount" if sys.platform == "darwin" else "mount"
    command = ["rclone", subcommand, f"{remote}:{root}", f"~/mnt/{connection.slug}"]
    if read_only:
        command.append("--read-only")
    else:
        command.extend(["--vfs-cache-mode", "writes"])
    command.append("--daemon")
    if sys.platform == "darwin":
        command.extend(["--daemon-wait", "15"])
    # The profile the remote federates through, for orientation.
    command.append(f"#  (AWS profile: {profile})")
    return command
