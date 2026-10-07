"""AWS CLI profile management.

Writes one named profile per connection into ~/.aws/config, pointing the
standard AWS tooling at the connection's storage role via
``web_identity_token_file`` — the tooling performs AssumeRoleWithWebIdentity
itself. Only profile blocks the CLI itself created are ever replaced; the
rest of the file (user profiles, comments, formatting) is preserved through
ConfigUpdater's round-trip editing.

Damaged configs written by older releases (values glued to the following
section header, duplicated managed profiles) are healed on load, before
parsing — a fused value otherwise parses as an option value and swallows
the section that follows it.
"""

from __future__ import annotations

import configparser
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from configupdater import ConfigUpdater

PROFILE_PREFIX = "cytario-"
AWS_CONFIG_PATH = Path.home() / ".aws" / "config"
MANAGED_COMMENT = "managed by cytario-cli"

# A value ending glued to the next section header, e.g.
# `web_identity_token_file = ...id_token[profile next]` — the newline between
# the last managed option and the next section was eaten by the old regex.
_FUSED_VALUE = re.compile(r"^(?P<value>[^\n=]*?)\[profile (?P<name>[^\]\n]+)\]\s*$")


@dataclass
class Connection:
    """One row of the /api/me/connections response."""

    name: str
    bucket_name: str
    prefix: str
    region: str
    s3_endpoint: str
    sts_endpoint: str
    role_arn: str | None
    access_level: str | None
    id: str | None = None

    @classmethod
    def from_api(cls, payload: dict) -> Connection:
        """Build one from a /api/me/connections row."""
        return cls(
            name=payload["name"],
            id=payload.get("id"),
            bucket_name=payload["bucketName"],
            prefix=payload.get("prefix", ""),
            region=payload["region"],
            s3_endpoint=payload["s3Endpoint"],
            sts_endpoint=payload["stsEndpoint"],
            role_arn=payload.get("roleArn"),
            access_level=payload.get("accessLevel"),
        )

    @property
    def slug(self) -> str:
        """Profile-safe lowercase slug of the connection name."""
        slug = re.sub(r"[^a-z0-9-]+", "-", self.name.lower()).strip("-")
        return slug or "connection"


def profile_name(connection: Connection) -> str:
    """Return the AWS CLI profile name for a connection."""
    return f"{PROFILE_PREFIX}{connection.slug}"


def _is_managed(section_name: str) -> bool:
    return section_name.startswith(f"profile {PROFILE_PREFIX}")


def _heal(text: str) -> str:
    """Repair damage older releases left in ~/.aws/config.

    Two steps, each a no-op on a healthy file:

    1. Split values fused to the following section header back into a
       newline, restoring the swallowed section. ConfigUpdater (like any
       INI parser) would otherwise treat ``...id_token[profile x]`` as one
       option value and the next section would never parse.
    2. Drop duplicated managed profile sections, keeping the first. On a
       fused/duplicated file every subsequent ``setup`` run appended
       another copy of the profile.
    """
    healed: list[str] = []
    seen: set[str] = set()
    skipping = False
    for line in text.splitlines():
        if skipping:
            if line.lstrip().startswith("["):
                skipping = False
            else:
                continue
        match = re.fullmatch(r"\[(profile [^\]]+)\]", line.strip())
        if match:
            section_name = match.group(1)
            if _is_managed(section_name) and section_name in seen:
                while healed and (healed[-1].lstrip().startswith("#") or not healed[-1].strip()):
                    healed.pop()
                skipping = True
                continue
            seen.add(section_name)
        if "=" in line:
            key, _, value = line.partition("=")
            fused = _FUSED_VALUE.fullmatch(value.strip())
            if fused:
                healed.append(f"{key}= {fused.group('value')}")
                healed.append(f"[profile {fused.group('name')}]")
                seen.add(f"profile {fused.group('name')}")
                continue
        healed.append(line)
    return "\n".join(healed) + ("\n" if text.endswith("\n") or healed else "")


def _parse(path: Path) -> ConfigUpdater:
    updater = ConfigUpdater(strict=False)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if not text.endswith("\n") and text.strip():
            text += "\n"
        updater.read_string(_heal(text))
    return updater


def _upsert(updater: ConfigUpdater, connection: Connection, token_file: Path) -> None:
    section_name = f"profile {profile_name(connection)}"
    if updater.has_section(section_name):
        section = updater[section_name]
    else:
        updater.add_section(section_name)
        section = updater[section_name]
        section.add_before.space(1)
        section.add_before.comment(MANAGED_COMMENT, comment_prefix="#")

    section["region"] = connection.region
    section["role_arn"] = connection.role_arn or ""
    section["web_identity_token_file"] = str(token_file)
    if "amazonaws.com" not in connection.s3_endpoint:
        section["endpoint_url"] = connection.s3_endpoint


def _validate(updater: ConfigUpdater, path: Path) -> None:
    """Re-parse what we are about to write and prove it is sane."""
    parser = configparser.RawConfigParser(strict=True)
    parser.read_string(str(updater))

    counts: dict[str, int] = {}
    for section_name in parser.sections():
        if _is_managed(section_name):
            counts[section_name] = counts.get(section_name, 0) + 1
    duplicates = [name for name, count in counts.items() if count > 1]
    if duplicates:
        raise ValueError(f"duplicate managed profiles in {path}: {', '.join(sorted(duplicates))}")

    for section_name in parser.sections():
        for option, value in parser.items(section_name):
            if re.search(r"\[profile .+\]", value):
                raise ValueError(f"fused value in {path}: {section_name}.{option}")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", text=False)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        tmp_path.chmod(0o600)
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_profile(connection: Connection, token_file: Path) -> str:
    """Upsert the AWS CLI profile block for a connection; return the profile name.

    Loads ~/.aws/config through ConfigUpdater, healing damage older releases
    left behind, replaces or adds the managed profile section, then writes
    atomically after validating the result. Sections the CLI does not own are
    never modified.
    """
    updater = _parse(AWS_CONFIG_PATH)
    _upsert(updater, connection, token_file)
    _validate(updater, AWS_CONFIG_PATH)
    _atomic_write(AWS_CONFIG_PATH, str(updater))
    return profile_name(connection)
