---
name: cytario-cli
description: This skill should be used when the user asks to "work with cytario data", "access cytario storage", "list my cytario connections", "download or upload files to a cytario bucket", "read analysis results from cytario", "use the cytario CLI", "set up AWS profiles for cytario", or when a task involves S3 data managed by Cytario (buckets, connections, imaging datasets, annotation sidecars, analysis outputs) that must be accessed programmatically as the signed-in user.
---

# Cytario CLI — agent workflow for cytario-managed data

The `cytario` CLI (Python, `pip install cytario-cli` or `uv tool install cytario-cli`)
lets you work with the data of the user's Cytario storage connections using the
workstation's standard AWS tooling — never with the user's browser credentials.

## Steps

1. **Sign in (once per session):** run `cytario auth login --host <cytario-host>`
   (the host is remembered; `CYTARIO_HOST` also works). A browser window opens for
   the user to sign in; the CLI receives the result on a loopback redirect and
   stores a refresh grant in `~/.config/cytario/cli/`. If the user has recently
   signed in on this machine, check first with `cytario auth status`.
2. **Discover the data:** `cytario connections list --json` returns every
   connection visible to the user with bucket, prefix, region, endpoints, and the
   user's access level (`read-only`, `annotate`, `read-write`, `admin`).
3. **Configure AWS profiles:** `cytario connections setup --all` (or with a
   connection name) writes one AWS CLI profile per connection
   (`~/.aws/config`, `[profile cytario-<name>]`) plus token files under
   `~/.aws/cytario/`. Standard tooling then does the federation itself.
4. **Work with the data** via the generated profiles:
   - `aws --profile cytario-<name> s3 ls s3://<bucket>/<prefix>`
   - `aws --profile cytario-<name> s3 cp ...`
   - boto3: `boto3.session.Session(profile_name="cytario-<name>")`
   - pandas/pyarrow: read Parquet/CSV after `s3 cp` to a scratch dir.
5. **Keep tokens fresh during long work:** ID tokens live ~1 hour. The AWS CLI
   re-reads the token file on every `AssumeRoleWithWebIdentity`, so before any
   operation expected to outlast a token (or on `ExpiredToken` errors) run
   `cytario auth refresh` to rewrite all token files from a fresh ID token.

## Rules

- **You act as the user, with exactly their grant.** A `read-only` connection
  must not be written to; `annotate` permits annotation sidecar writes only;
  bucket *sharing* (`s3:PutBucketPolicy`) is always denied outside the web app.
  Never try to widen access — surface the access level instead.
- **Never handle the user's browser session, password, or the refresh grant
  file** (`~/.config/cytario/cli/state.json`). The CLI owns them.
- **Never write long-lived AWS access keys.** The profiles use
  `web_identity_token_file`; the credential mint happens per operation.
- Prefer `connections list --json` (machine-readable) over parsing table output.
- Name-locality: profiles are `cytario-<connection-name>`; token files
  `~/.aws/cytario/<connection-name>/id_token`.
- If `auth token`/`connections list` returns "rejected", the grant was revoked
  or expired — ask the user to sign in again; do not retry in a loop.
