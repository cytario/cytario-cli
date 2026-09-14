# AGENTS.md — cytario-cli

CLI for working with Cytario storage connections as the signed-in user.
Python ≥ 3.10, Typer, uv, hatchling. Published to public PyPI as `cytario-cli`.

## Commands

```bash
uv sync                       # install deps
uv run ruff format --check    # format gate
uv run ruff check             # lint gate (ALL, see pyproject ignore list)
uv run pytest                 # test gate
uv run cytario --help         # run the CLI from the working tree
uv build                      # sdist + wheel
```

## Conventions

- Conventional Commits; releases cut by python-semantic-release on push to
  main (no release commits — tag + GitHub Release only; PyPI via trusted
  publishing in the `pypi` environment).
- npm-style comment discipline as the rest of Cytario: minimal comments, no
  ticket IDs in code, no history comments.
- The skill file `skills/cytario-cli.md` documents the agent workflow; keep it
  in sync with the CLI's commands.
- Security invariants (do not weaken):
  - public client only — no client secret is ever shipped or stored;
  - the refresh grant and token files are `0600` under `~/.config/cytario/cli`
    and `~/.aws/cytario/`;
  - no long-lived AWS keys are written; profiles use `web_identity_token_file`;
  - the CLI never proxies data — the workstation's AWS tooling talks to the
    storage provider directly under the user's own grant.
