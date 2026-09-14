"""AWS CLI profile management.

Writes one named profile per connection into ~/.aws/config, pointing the
standard AWS tooling at the connection's storage role via
``web_identity_token_file`` — the tooling performs AssumeRoleWithWebIdentity
itself. Only profile blocks the CLI itself created are ever replaced; the
rest of the file (user profiles, comments, formatting) is preserved
byte-for-byte.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

PROFILE_PREFIX = "cytario-"
AWS_CONFIG_PATH = Path.home() / ".aws" / "config"


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

    @classmethod
    def from_api(cls, payload: dict) -> Connection:
        """Build one from a /api/me/connections row."""
        return cls(
            name=payload["name"],
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


def _section_header(profile: str) -> str:
    return f"[profile {profile}]"


def _profile_block(connection: Connection, token_file: Path) -> str:
    """Render the managed profile block (with its own comment header)."""
    lines = [
        "# managed by cytario-cli",
        _section_header(profile_name(connection)),
        f"region = {connection.region}",
        f"role_arn = {connection.role_arn or ''}",
        f"web_identity_token_file = {token_file}",
    ]
    # S3-compatible providers need explicit endpoints; AWS S3 works by default.
    if "amazonaws.com" not in connection.s3_endpoint:
        lines.append(f"endpoint_url = {connection.s3_endpoint}")
    return "\n".join(lines)


def write_profile(connection: Connection, token_file: Path) -> str:
    """Upsert the AWS CLI profile block for a connection; return the profile name.

    Surgical text-level replace: the block (comment + section) is replaced or
    appended in place, leaving every other byte of the file untouched.
    """
    AWS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = AWS_CONFIG_PATH.read_text(encoding="utf-8") if AWS_CONFIG_PATH.exists() else ""

    block = _profile_block(connection, token_file)

    # Match an optional preceding managed-comment plus the section body up to
    pattern = re.compile(
        r"(?:^# managed by cytario-cli\n)?^\[profile "
        + re.escape(profile_name(connection))
        + r"\]\n(?:(?!\[).*\n?)*",
        re.MULTILINE,
    )
    if pattern.search(existing):
        updated = pattern.sub(block, existing, count=1)
    else:
        separator = "" if not existing or existing.endswith("\n") else "\n"
        updated = f"{existing}{separator}{block}\n"

    AWS_CONFIG_PATH.write_text(updated, encoding="utf-8")
    return profile_name(connection)
