"""Local configuration and token storage.

The CLI stores its host, the OIDC endpoints, and the refresh grant in a
user-private directory (~/.config/cytario/cli). Token files consumed by the
AWS CLI (web_identity_token_file) live under ~/.aws/cytario/<profile>/.
Everything is written with 0600 permissions.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

CLIENT_ID = "cytario-cli"
SCOPE = "openid profile organization"
SCOPE_OFFLINE = f"{SCOPE} offline_access"

CONFIG_DIR = Path(os.environ.get("CYTARIO_CLI_HOME", Path.home() / ".config" / "cytario" / "cli"))
STATE_FILE = CONFIG_DIR / "state.json"
TOKENS_DIR = Path(os.environ.get("CYTARIO_CLI_AWS_HOME", Path.home() / ".aws" / "cytario"))

FILE_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600


@dataclass
class CliState:
    """Persisted between invocations: the host and the refresh grant."""

    host: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    refresh_token: str
    id_token: str | None = None
    id_token_expires_at: float = 0.0  # epoch seconds; 0 = unknown

    @classmethod
    def load(cls) -> CliState | None:
        """Read the persisted state; None when absent or truncated."""
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return cls(
                host=data["host"],
                issuer=data["issuer"],
                authorization_endpoint=data["authorization_endpoint"],
                token_endpoint=data["token_endpoint"],
                refresh_token=data["refresh_token"],
                id_token=data.get("id_token"),
                id_token_expires_at=data.get("id_token_expires_at", 0.0),
            )
        except KeyError:
            return None

    def save(self) -> None:
        """Persist the state to the user-private state file (0600)."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(self.__dict__), encoding="utf-8")
        STATE_FILE.chmod(FILE_MODE)

    def delete(self) -> None:
        """Remove the persisted state (signed-out / dead grant)."""
        STATE_FILE.unlink(missing_ok=True)


def write_token_file(profile_slug: str, id_token: str) -> Path:
    """Write the ID token for one connection profile and return its path."""
    TOKENS_DIR.mkdir(parents=True, exist_ok=True)
    token_dir = TOKENS_DIR / profile_slug
    token_dir.mkdir(parents=True, exist_ok=True)
    token_file = token_dir / "id_token"
    token_file.write_text(id_token, encoding="utf-8")
    token_file.chmod(FILE_MODE)
    return token_file
