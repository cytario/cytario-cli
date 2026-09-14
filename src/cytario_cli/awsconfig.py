"""AWS CLI profile management.

Writes one named profile per connection into ~/.aws/config, pointing the
standard AWS tooling at the connection's storage role via
``web_identity_token_file`` — the tooling performs AssumeRoleWithWebIdentity
itself. Only profile blocks the CLI itself created are ever replaced.
"""

from __future__ import annotations

import configparser
import re
from dataclasses import dataclass
from pathlib import Path

PROFILE_PREFIX = "cytario-"
MANAGED_HEADER = "# managed by cytario-cli"
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


def _section_name(profile: str) -> str:
    return f"profile {profile}"


def write_profile(connection: Connection, token_file: Path) -> str:
    """Upsert the AWS CLI profile block for a connection; return the profile name."""
    config = configparser.RawConfigParser()
    config.optionxform = str  # type: ignore[method-assign]  # keep case of AWS keys
    if AWS_CONFIG_PATH.exists():
        config.read(AWS_CONFIG_PATH, encoding="utf-8")

    section = _section_name(profile_name(connection))
    if not config.has_section(section):
        config.add_section(section)
    config.set(section, "region", connection.region)
    config.set(section, "role_arn", connection.role_arn or "")
    config.set(section, "web_identity_token_file", str(token_file))
    # S3-compatible providers need explicit endpoints; AWS S3 works by default.
    is_aws_s3 = "amazonaws.com" in connection.s3_endpoint
    if not is_aws_s3:
        config.set(section, "endpoint_url", connection.s3_endpoint)

    AWS_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with AWS_CONFIG_PATH.open("w", encoding="utf-8") as handle:
        handle.write(f"{MANAGED_HEADER}\n")
        config.write(handle)
    return profile_name(connection)
