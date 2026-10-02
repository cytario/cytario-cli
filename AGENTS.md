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

- Conventional Commits, and **the commit type is the release trigger** —
  releases are cut by python-semantic-release on push to main (no release
  commits — tag + GitHub Release only; PyPI via trusted publishing in the
  `pypi` environment). Type → version:

  | Commit | Release |
  |---|---|
  | `feat: …` (or `feat!:` / `feat(scope)!:` / a `BREAKING CHANGE:` footer) | **minor** (major if breaking) |
  | `fix: …` / `perf: …` | **patch** |
  | `docs:`, `chore:`, `style:`, `refactor:`, `test:`, `ci:` | **nothing — no release, no tag** |

  Rules for agents:
  - **Every PR that ships anything a user or agent of the CLI can observe —
    code, the packaged skill file, docs that change behavior — must merge via
    `fix:` or `feat:` (or a breaking variant).** A `docs:`/`chore:` commit is
    silently swallowed: it never releases, so the change never reaches PyPI
    or the installed `cytario skill install` copies.
  - Do not use `docs:` or `chore:` to describe a change merely because it
    touches text — the skill file ships in the wheel, so updating it is a
    `fix:`/`feat:` like any other shipped artifact.
  - Breaking changes: `feat!:`/`fix!:` or a `BREAKING CHANGE:` footer.
  - Squash merges are parsed by their title (`parse_squash_commits = true`);
  merge commits are ignored (`ignore_merge_commits = true`) — the type on
  the squashed title is what counts.
- npm-style comment discipline as the rest of Cytario: minimal comments, no
  ticket IDs in code, no history comments.
- The skill file `src/cytario_cli/skills/cytario-cli.md` documents the agent
  workflow; keep it in sync with the CLI's commands. It ships in the package
  as data, and `cytario skill install` distributes it — update it here, never
  edit installed copies.
- Security invariants (do not weaken):
  - public client only — no client secret is ever shipped or stored;
  - the refresh grant and token files are `0600` under `~/.config/cytario/cli`
    and `~/.aws/cytario/`;
  - no long-lived AWS keys are written; profiles use `web_identity_token_file`;
  - the CLI never proxies data — the workstation's AWS tooling talks to the
    storage provider directly under the user's own grant.
