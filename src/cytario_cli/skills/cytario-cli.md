---
name: cytario-cli
description: This skill should be used when the user asks to "work with cytario data", "access cytario storage", "list my cytario connections", "download or upload files to a cytario bucket", "read analysis results from cytario", "use the cytario CLI", "set up AWS profiles for cytario", or when a task involves S3 data managed by Cytario (buckets, connections, imaging datasets, annotations, view settings, analysis results, saved job configs) that must be accessed programmatically as the signed-in user.
---

# Cytario CLI — agent workflow for cytario-managed data

Cytario is a web-based platform for browsing, viewing, and analyzing digital
pathology data — whole-slide images, multi-channel fluorescence, and the
tabular and segmentation data around them — stored in S3-compatible object
storage. Users connect their own buckets as storage connections; annotations,
view settings, analysis results, and job configs all live in those buckets
alongside the images. Research-use-only software for pathologists,
bioinformaticians, and administrators.

The `cytario` CLI (Python, `pipx install cytario-cli` or `uv tool install cytario-cli`)
lets you work with the data of the user's Cytario storage connections using the
workstation's standard AWS tooling — never with the user's browser credentials.

## Steps

1. **Sign in (once per session):** run `cytario auth login --host <cytario-web-host>`
   (e.g. `https://app.cytario.com` — the **web app** host, not the identity host;
   the host is remembered; `CYTARIO_HOST` also works). A browser window opens for
   the user to sign in; the CLI receives the result on a loopback redirect and
   stores a refresh grant in `~/.config/cytario/cli/`. On a machine with no
   browser (a remote workspace, an SSH session), the CLI prints the
   authorization URL plus the loopback port instead — the user must forward
   that port to a device with a browser (e.g. `ssh -L <port>:127.0.0.1:<port>`)
   and open the URL there. If the user has recently
   signed in on this machine, check first with `cytario auth status --json` —
   it returns the signed-in user's Keycloak `sub` (`userId`), `email`, and
   `name`. You need that `sub` before touching any `settings.*.json` sidecar
   (see "View settings" below).
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
   - Parquet/GeoParquet (results, overlays): query directly with DuckDB —
     through a DuckDB MCP server if available, else locally
     (`python -c "import duckdb; …"` or the `duckdb` CLI) on files `s3 cp`'d
     to a scratch dir; pandas/pyarrow work too but lack geometry handling.
5. **Keep tokens fresh during long work:** ID tokens live ~1 hour. The AWS CLI
   re-reads the token file on every `AssumeRoleWithWebIdentity`, so before any
   operation expected to outlast a token (or on `ExpiredToken` errors) run
   `cytario auth refresh` to rewrite all token files from a fresh ID token.

## How Cytario data is laid out in the bucket

Expect images and their companions under the connection prefix; some of these
are platform-owned files the web app reads and writes — treat them as data you
may read, but only modify when the user asks for it.

### Annotations — `<image_base>.annotations.<setId>.json`

Per **image**, beside it (`slide.ome.tif` → `slide.annotations.<setId>.json`).
One file per annotation **set**; the owner segment is a set UUID (name in the
file's `cytario.name`). Content: a GeoJSON `FeatureCollection` with a `cytario`
envelope (`schemaVersion: "1.0"`, `kind: "annotations"`, the image's s3Uri,
`coordinateSpace: "pixel"`, `pyramidLevel: 0`) — geometry in **level-0 pixel
coordinates**; each feature needs a non-empty string `id` (RFC 7946). Glob for
all sets of an image with `*.annotations.*.json`.

### View settings — `settings.<userId>.json`

Per **directory**, not per image: `settings.<owner>.json` in the image's
directory, one file **per user** — the `<userId>` segment is the owner's
Keycloak `sub`, and a directory can hold **other users'** settings files (their
shared view presets), not just the current user's. Each file holds that user's
**shared** view presets (channel colors/contrast, opacity, overlay
configurations) as JSON with a `cytario` envelope (`schemaVersion: "1.1"`,
`kind: "settings"`, `author` — the owner's `sub`). The live working state is
browser-local and never written to S3; read-only connections write no settings
at all. Glob with `settings.*.json`.

The viewer supports **at most 10 active channels at the same time** — never
author a preset with more, it renders a black canvas.

When writing view presets:

1. Resolve the current user's `sub` first (`cytario auth status --json` →
   `userId`) and write to `settings.<sub>.json` with `author: <sub>`.
2. **Never assume an existing settings file is the user's** — match its
   filename against the current `sub` before writing; on a mismatch it is
   someone else's shared views.
3. A view written into another user's file is misattributed: the viewer
   treats it as foreign and forks it on edit. If you discover you wrote into
   the wrong file, restore the original.
4. When editing any shared sidecar, keep the other users' views
   byte-identical — touch only the entries you mean to change.

### Analysis results — user-chosen output prefix, many shapes

Job outputs go to an output prefix chosen at submission (default when launching
from a node: `<input_prefix>/output/<input_name>/`; often under a `results/`
folder). Formats vary by app: **GeoParquet 1.1** is the recommended object-table
format (geometry column in WKB or WKT, plus a bbox covering — a `bbox` STRUCT
or flat `xmin/xmax/ymin/ymax` columns), plain Parquet, CSV, GeoJSON, HDF5
(deprecated), QC tiles. For overlay rendering the app auto-detects id
(`object`/`id`/`cell_id`/`label`), geometry (`geom`/`geometry`/`wkt`/…), and
centroid x/y columns; boolean class columns are prefixed `marker_positive_`.
Results are user data — read and analyze freely.

### Job configs — `configs/<name>.yaml`

Saved image-processing (compute job) configurations, YAML under the connection
prefix, conventionally `configs/<name>.yaml`: `applicationId`, `version`,
`parameters`, `input {connectionId, path}`, `output {connectionId, path}`,
optional `resources`. Saving them through the web app needs `read-write` or
`admin` (annotate allows sidecars only). Runtime job parameters travel as
container env vars (`CYTARIO_PARAMETERS`, `CYTARIO_OUTPUT_URI`), not as bucket
files.

## Reading image data efficiently (range reads, never bulk downloads)

Whole-slide and multiplex images are **multi-GB**; their bytes live in S3 and
the viewer never needs them locally.

- **Never bulk-download image data (`s3 cp` of an image) unless the user
  explicitly asks for the file.** Reading pixels or metadata remotely via HTTP
  range requests is the intended workflow: `boto3`
  `get_object(Range="bytes=<start>-<end>")` on the profile session (or
  `aws s3api get-object --range ...`).
- Sidecars, READMEs, annotation sets, settings, job configs, and tabular
  results (KB–MB) are fair game for `s3 cp`.
- TIFF strips are **scattered across the file** — a block-cached range reader
  (fetching aligned blocks around each request) re-reads massively for
  scattered strip data. Fetch **exact** strip byte ranges from the IFD's strip
  offsets/counts instead of caching blocks.

### QPTIFF / multiplex IF recipe

For Akoya QPTIFF (and similar multiplex TIFFs):

- **Channel keys:** each full-resolution page's `ImageDescription` carries QPI
  XML; the channel name is the `<Biomarker>` value when meaningful (the
  biological marker — this is the channel key), falling back to `<Name>` (the
  fluorophore/filter, e.g. DAPI, Cy5); placeholders (`--`, `none`, `n/a`) are
  skipped. `<Name>` differing from the channel name is the `Fluor`; `<Color>`
  is `R,G,B` 0–255. This must match `qptiff-loader/src/metadata/mapChannels.ts`.
- **Channel name collisions:** duplicates get a " (n)" suffix in first-seen
  order (`DAPI`, `DAPI (2)`).
- **Page layout:** N full-resolution channels first, then the label image,
  then the pyramid (downsampled) levels in channel order, **smallest level
  last**. Read the last page for per-channel statistics.
- **Contrast limits:** the app's tuned per-channel display limits track the
  **p99.9 of the smallest pyramid level** — that's the auto-contrast recipe
  when authoring view presets.
- **Check for a `<image>.README.txt` companion first** — Akoya exports often
  carry the panel story (marker list, fluorophores, cycle structure) there.

## Rules

- **Data-transfer etiquette:** never bulk-download image data unless
  explicitly asked; use range reads (see above).
- **You act as the user, with exactly their grant.** A `read-only` connection
  cannot be written to; `annotate` permits annotation and settings sidecar
  writes only; `read-write`/`admin` cover the connection's whole prefix.
  Bucket *sharing* (`s3:PutBucketPolicy`) is always denied through the CLI.
  Never try to widen access — surface the access level instead.
- **Never handle the user's browser session, password, or the refresh grant
  file** (`~/.config/cytario/cli/state.json`). The CLI owns them.
- **Never write long-lived AWS access keys.** The profiles use
  `web_identity_token_file`; the credential mint happens per operation.
- **Do not present platform sidecars as user data**: `*.annotations.*.json`,
  `settings.*.json`, and `configs/*.yaml` are hidden from the web app's
  directory listings; when listing a bucket for the user, note them as
  Cytario-managed companions, not content. Only edit them when the user asks.
- Prefer `connections list --json` (machine-readable) over parsing table output.
- Name-locality: profiles are `cytario-<connection-name>`; token files
  `~/.aws/cytario/<connection-name>/id_token`.
- If `auth token`/`connections list` returns "rejected", the grant was revoked
  or expired — ask the user to sign in again; do not retry in a loop.
