# cytario-cli

CLI for working with [Cytario](https://github.com/cytario) storage connections
as the signed-in user — designed for AI agents and analysts who need to work
with connection data using the workstation's standard AWS tooling.

## What it does

- **`cytario auth login --host <url>`** — browser sign-in (OAuth 2.0
  Authorization Code + PKCE against the deployment's identity service; a
  loopback redirect receives the result). The refresh grant is stored
  user-private (`~/.config/cytario/cli`, `0600`).
- **`cytario connections list [--json]`** — the connections visible to you,
  with the storage role and access level your grant resolves to.
- **`cytario connections setup [--all | <name>]`** — writes one AWS CLI profile
  per connection into `~/.aws/config` (`[profile cytario-<name>]`) with
  `web_identity_token_file`, so `aws` / boto3 / pandas perform
  `AssumeRoleWithWebIdentity` themselves — with exactly your grant's
  authorization, never wider.
- **`cytario auth token` / `cytario auth refresh`** — a fresh ID token on
  stdout, and a rewrite of all managed token files (tokens live ~1 hour).

Agents: see [`skills/cytario-cli.md`](skills/cytario-cli.md) for the
tool-neutral agent workflow shipped with this repo — what Cytario is,
how its data (annotations, view settings, results, job configs) is laid
out in your buckets, and the CLI workflow on top.

## Installing the skill

The skill file is plain Markdown with a `name`/`description` front-matter
envelope and is not part of the PyPI package. To use it with an agent that
picks up skills from a directory (Claude Code, Cline, any tool that scans a
skills folder), fetch it from the repo and point your tool at it, e.g.:

```bash
mkdir -p ~/.claude/skills   # or your tool's skills directory
curl -fsSL -o ~/.claude/skills/cytario-cli.md \
  https://raw.githubusercontent.com/cytario/cytario-cli/main/skills/cytario-cli.md
```

Replace `main` with a release tag to pin a version. For tools without a
skills mechanism, paste the file's contents into your agent's system prompt
or project instructions — the file is self-contained.

## Install

```bash
uv tool install cytario-cli   # or: pip install cytario-cli
```

Python ≥ 3.10. The CLI is a public OIDC client — no secrets are shipped or
stored beyond your own refresh grant.

## Usage

```bash
cytario auth login --host https://app.cytario.com
cytario connections list
cytario connections setup --all
aws s3 ls --profile cytario-mybucket
```

Host resolution order: `--host`, then `CYTARIO_HOST`, then the last
signed-in host. The host is the **cytario web app** (e.g.
`https://app.cytario.com`) — not the identity host (`https://auth.cytario.com`);
the CLI derives the identity endpoints from it automatically.

## Security model

- Your authorization is exactly your Cytario grant on each connection
  (`read-only` / `annotate` / `read-write` / `admin`); bucket sharing stays
  confined to the web app.
- No long-lived AWS keys are written; the AWS CLI federates per operation
  from the token file, and the CLI refreshes tokens before expiry.
- Revocation: revoking the CLI session in the identity service (or removing
  your group membership) ends the CLI's access within one token lifetime.

## Development

```bash
uv sync
uv run ruff format --check && uv run ruff check
uv run pytest
```

Releases are cut by [python-semantic-release](https://python-semantic-release.readthedocs.io/)
on Conventional Commits and published to PyPI via trusted publishing.

## License

MIT — see [LICENSE](LICENSE).
