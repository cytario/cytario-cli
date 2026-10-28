"""AWS CLI profile management.

Writes one named profile per connection into ~/.aws/config, pointing the
standard AWS tooling at the connection's storage role via
``web_identity_token_file`` — the tooling performs AssumeRoleWithWebIdentity
itself. ARN-less providers (RustFS) get a placeholder ``role_arn`` (botocore
requires the field present; the provider ignores it — entitlement comes from
the OIDC token) plus ``endpoint_url`` so federation and data calls both hit
the provider's own S3-compatible endpoint. Public (credential-less)
connections get no profile at all — a config file cannot disable signing, and
the CLI never writes static keys — so they are the caller's skip with a
note. Only profile blocks the CLI itself created are ever replaced; the rest
of the file (user profiles, comments, formatting) is preserved through
ConfigUpdater's round-trip editing. ConfigUpdater is kept non-strict so a
damaged file (duplicate sections left by older releases) still loads — the
validation step then reports it instead of silently rewriting.
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
# ARN-less providers (RustFS) still need a syntactically valid role_arn in the
# profile — botocore refuses to run the web-identity flow without one, while
# the provider never reads the field (entitlement comes from the OIDC token).
PLACEHOLDER_ROLE_ARN = "arn:aws:iam:::role/cytario-rustfs"
# The platform synthesizes exactly this grant for a public (credential-less)
# connection: read-only with a null role ARN, regardless of the stored level.
PUBLIC_ACCESS_LEVEL = "read-only"


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

    @property
    def has_grant(self) -> bool:
        """Whether the API resolved an applicable grant for the user.

        AWS grants carry a role ARN; ARN-less providers (RustFS) carry only the
        access level. Either signals a grant — a row with neither has none.
        """
        return bool(self.role_arn or self.access_level)

    @property
    def is_public(self) -> bool:
        """Whether the connection is public (credential-less and read-only).

        The platform resolves a public connection's grant to read-only with a
        null role ARN — the bucket's own public-read policy is the entire
        entitlement. The API shape is identical to an ARN-less RustFS grant,
        so the endpoint is the discriminator: an AWS-shaped endpoint with a
        null-ARN read-only grant can only be the public provider (RustFS
        always serves its own S3-compatible endpoint, and any ARN-less AWS
        row would be a corrupt mint).
        """
        return (
            not self.role_arn
            and self.access_level == PUBLIC_ACCESS_LEVEL
            and _is_aws_endpoint(self.s3_endpoint)
        )

    @property
    def can_setup(self) -> bool:
        """Whether a working AWS profile can be written for the connection.

        An AWS S3 grant needs its role ARN (fail closed, mirroring the
        platform's own AWS mint); an ARN-less grant is only usable on a
        non-AWS endpoint, where the provider never reads the field. A public
        connection is never settable: no profile can express unsigned reads,
        so `rclone setup` is its local-access path instead.
        """
        if self.is_public:
            return False
        if self.role_arn:
            return True
        return bool(self.access_level) and not _is_aws_endpoint(self.s3_endpoint)


def profile_name(connection: Connection) -> str:
    """Return the AWS CLI profile name for a connection."""
    return f"{PROFILE_PREFIX}{connection.slug}"


def _is_managed(section_name: str) -> bool:
    return section_name.startswith(f"profile {PROFILE_PREFIX}")


def _is_aws_endpoint(endpoint: str) -> bool:
    return "amazonaws.com" in endpoint


def _parse(path: Path) -> ConfigUpdater:
    updater = ConfigUpdater(strict=False)
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if not text.endswith("\n") and text.strip():
            text += "\n"
        updater.read_string(text)
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
    role_arn = connection.role_arn
    if not role_arn and not _is_aws_endpoint(connection.s3_endpoint):
        role_arn = PLACEHOLDER_ROLE_ARN
    section["role_arn"] = role_arn or ""
    section["web_identity_token_file"] = str(token_file)
    if not _is_aws_endpoint(connection.s3_endpoint):
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
