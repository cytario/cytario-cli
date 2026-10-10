---
name: cytario-cli
description: This skill should be used when the user asks to "work with cytario data", "access cytario storage", "list my cytario connections", "download or upload files to a cytario bucket", "read analysis results from cytario", "use the cytario CLI", "set up AWS profiles for cytario", "mount cytario storage locally", "browse a cytario bucket as a local folder or drive", "use rclone with cytario", or when a task involves S3 data managed by Cytario (buckets, connections, imaging datasets, annotations, view settings, analysis results, saved job configs) that must be accessed programmatically as the signed-in user.
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
3. **Read image metadata and contrast limits:**
   `cytario image describe s3://<bucket>/<key>` prints an image's dimensions,
   pixel type, level count, channel keys/names/fluors/colors, and the
   per-channel contrast limits as JSON. It opens the web app's describe page
   in the browser (the user must be signed in) and receives the result on a
   loopback redirect — this is the authoritative source for channel naming
   and contrast values, because it uses the platform's own loaders and
   auto-contrast recipe. On a deployment whose web app predates the route
   the command says so and exits; fall back to the hand-rolled recipe below.
   The command is **expensive** — a browser round trip through the web app
   plus a possible token refresh — and its output is often needed by both
   you and a script (e.g. generating view presets). **Redirect the first
   call to a file** (`cytario image describe s3://… > describe.json`), then
   read the file for yourself and have the script parse it — never re-run
   the command for a second consumer, and never hand-transcribe channel
   tables into code. Status messages (token refresh, no-browser hints) go
   to stderr, so the redirected file is pure JSON.
4. **Configure AWS profiles:** `cytario connections setup --all` (or with a
   connection name) writes one AWS CLI profile per connection
   (`~/.aws/config`, `[profile cytario-<name>]`) plus token files under
   `~/.aws/cytario/`. Standard tooling then does the federation itself.
5. **Mount a connection locally (optional):** `cytario rclone setup --all`
   writes one rclone remote per connection (`[cytario-<name>]`, `type = s3`,
   `env_auth = true`, `profile = cytario-<name>`) into the rclone config —
   no credentials stored, the remote federates through the AWS profile from
   step 4, and the command refreshes that profile/token file as part of the
   run. It then prints a ready-to-run `mount:` command; see
   "Mounting with rclone" below for the OS-specific details and flags before
   running or suggesting it.
6. **Work with the data** via the generated profiles:
   - `aws --profile cytario-<name> s3 ls s3://<bucket>/<prefix>`
   - `aws --profile cytario-<name> s3 cp ...`
   - boto3: `boto3.session.Session(profile_name="cytario-<name>")`
   - Parquet/GeoParquet (results, overlays): query directly with DuckDB —
     through a DuckDB MCP server if available, else locally
     (`python -c "import duckdb; …"` or the `duckdb` CLI) on files `s3 cp`'d
     to a scratch dir; pandas/pyarrow work too but lack geometry handling.
     Over a mount (step 5) the same files are plain paths — no `s3 cp` needed.
7. **Keep tokens fresh during long work:** ID tokens live ~1 hour. The AWS CLI
   re-reads the token file on every `AssumeRoleWithWebIdentity`, so before any
   operation expected to outlast a token (or on `ExpiredToken` errors) run
   `cytario auth refresh` to rewrite all token files from a fresh ID token.
   This applies to long-running mounts too — see "Mounting with rclone".

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
shared view presets), not just the current user's. Each file holds that
user's **shared** view presets (channel visibility/colors/contrast, opacities,
outline toggles) as JSON with a `cytario` envelope. The live working state is
browser-local and never written to S3; read-only connections write no settings
at all. Glob with `settings.*.json`.

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

#### Settings-file schema

Generate settings files from this schema — do **not** copy a settings file
found somewhere in the bucket as a template: older files can carry an older
`schemaVersion` or fields the current viewer discards. The reader accepts
`schemaVersion` `"1.0"`, `"1.1"`, and `"1.2"`; **always write `"1.2"`**.
Overlay state stopped being persisted in 1.2 (it is machine-local now), so a
hand-authored file contains no overlay fields; unknown keys are stripped on
load.

The document is a JSON object with exactly two top-level keys:

- `cytario` — the envelope: `schemaVersion: "1.2"`, `kind: "settings"`,
  `image` (string — the s3Uri of any image in the directory; it identifies
  the directory, not one specific image), `author` (string — the owner's
  `sub`, identical to the filename segment).
- `views` — array of the owner's shared views. The whole file is rewritten
  on each save, so when editing an existing file carry every existing view
  forward unchanged.

Each element of `views`:

- `id` — string, UUID, unique within the file.
- `author` — string — the owner's `sub`.
- `name` — string, optional; omit the key entirely when the view has no
  name (never write `null`).
- `shared` — boolean; shared views are the only ones persisted to S3, so
  write `true`.
- `channels` — object keyed by the image's **channel keys** (the channel
  names from `cytario image describe`), each value:
  - `isVisible` — boolean, optional;
  - `contrastLimits` — `[min, max]` numbers, optional — the channel's
    display intensity domain; take real values from `cytario image
    describe` (or the contrast recipe below) rather than guessing;
  - `color` — `[r, g, b]` numbers 0–255, optional — a **numeric RGB
    triple, never a hex string**.
- `channelsOpacity` — number, 0–1 (default 1).
- `showCellOutline` — boolean (default true).
- `annotationsOpacity` — number, 0–1 (default 1).
- `showAnnotationOutline` — boolean (default true).

The viewer supports **at most 10 active channels at the same time** — never
author a preset with more than 10 `isVisible: true` channels, it renders a
black canvas.

Minimal example (one view, two channels):

```json
{
  "cytario": {
    "schemaVersion": "1.2",
    "kind": "settings",
    "image": "s3://bucket/data/slide.ome.tif",
    "author": "3f8a1b2c-…-keycloak-sub"
  },
  "views": [
    {
      "id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
      "author": "3f8a1b2c-…-keycloak-sub",
      "name": "Tumor microenvironment",
      "shared": true,
      "channels": {
        "DAPI": { "isVisible": true, "contrastLimits": [0, 65535], "color": [0, 114, 189] },
        "CD8": { "isVisible": true, "contrastLimits": [100, 3000], "color": [237, 28, 36] }
      },
      "channelsOpacity": 1,
      "showCellOutline": true,
      "annotationsOpacity": 1,
      "showAnnotationOutline": true
    }
  ]
}
```

#### Generating views programmatically (one pass, one script)

When a script will consume a command's output — e.g. `image describe`
for channel keys, contrast limits, and colors when authoring view
presets — redirect the **first** invocation to a file:

```bash
cytario image describe s3://<bucket>/<key> > describe.json
```

then read the file yourself. Never re-run an expensive command to feed
a second consumer; the describe route round-trips through the web app's
describe page, a loopback redirect, and possibly a token refresh — it
is not a cheap call. Status lines (`Refreshing tokens...`, no-browser
forwarding hints) go to stderr, so the redirected file is pure JSON;
still, have the generator script parse it defensively (strip to the
first `{`) rather than running a separate cleanup invocation.

One script, one invocation: the generator builds the document, then
**asserts its own output** before writing — `schemaVersion` is `"1.2"`,
`author` matches the filename segment, every view has ≤10
`isVisible: true` channels, colors are numeric RGB triples, view ids
are unique UUIDs — and prints a one-line summary per view. Extra
`python3 -c` validation round-trips cost a permission prompt each;
asserts belong inside the script next to the data they check.

#### Naming views

Give each view a short, meaningful name describing the cell population,
phenotype, or biological perspective it shows — e.g. **"Tumor
microenvironment"**, "CD8+ T cells", "Tissue architecture". Avoid generic
names ("View 1", "Preset 2") and raw channel lists ("DAPI + CD8"): shared
views surface by name in the viewer, and the name is what tells a colleague
what the preset was built to show.

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

### QPTIFF / multiplex IF recipe (fallback for older deployments)

Prefer `cytario image describe` (step 3) — it reads metadata and contrast
limits through the platform's real loaders. Hand-roll the recipe below only
when the deployment's web app predates the describe route, for Akoya QPTIFF
(and similar multiplex TIFFs):

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

## Mounting with rclone

`cytario rclone setup --all` (or with a connection name) writes an rclone
remote per connection so the whole bucket prefix can be mounted as a local
filesystem. The remote stores **no credentials**: `env_auth = true` +
`profile = cytario-<name>` makes rclone federate through the AWS CLI profile
(`web_identity_token_file`), exactly like `aws s3` does — per operation, with
exactly the user's grant. The setup command prints a ready-to-run mount
command for the detected platform; the OS differences below matter.

### Per-OS mounting

Always check the OS before suggesting or running a mount — the right command
differs:

- **macOS:** use `rclone nfsmount`, not `rclone mount` — rclone itself
  recommends it on macOS, and it avoids macFUSE/FUSE-T problems (FUSE-T +
  Finder mutates file mtimes, which can trigger full re-uploads; `--read-only`
  can fail silently). Without `--vfs-cache-mode`, an NFS mount is **read-only**.
  Unmount with `umount` or `diskutil unmount`, not `fusermount`.
- **Linux:** `rclone mount` is the battle-tested default; `--daemon`
  backgrounds it. Unmount with `fusermount -u` or `umount`.
- **Windows:** mounts work **only with WinFsp installed**
  (https://winfsp.dev/rel/). Check for it before suggesting a mount — without
  it, fall back to the `aws s3` workflows above and tell the user why.
  `--daemon` is unsupported (foreground only); mount to a drive letter (`X:`)
  or a directory path. `nfsmount` and `mount` behave the same on Windows.

### Flags that matter

- `--vfs-cache-mode writes` — needed for normal write support (read+write
  opens, random writes, retried uploads); without it, only sequential
  writes work — and on macOS NFS mounts, nothing writes at all.
- `--read-only` — for `read-only` connections; denies writes at the filesystem
  level, matching the grant (never try to widen it).
- `--daemon` / `--daemon-wait` — background the mount (Unix only); on macOS
  pass a reasonable `--daemon-wait` (the default sleep matters there).
- Reading multi-GB images through a mount works without a cache: VFS chunked
  reading serves random-access range reads directly. Do not reach for
  `--vfs-cache-mode full` unless the user needs it — it copies reads to disk.

### Long-running mounts

- ID tokens live ~1 hour; rclone re-reads the token file when its cached
  credentials expire, so run `cytario auth refresh` during long sessions —
  expired-token failures on a mounted path mean refresh (and if the grant was
  revoked, re-login).
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` in the environment silently
  override the named profile — unset them for the mount process.
- Remote names are `cytario-<connection-name>`; the mount root is
  `<remote>:<bucket>/<prefix>`. `rclone listremotes` shows what's configured.
- Re-running `cytario rclone setup` refreshes both the AWS profile and the
  remote in one pass, so an expired or hand-edited config self-heals on the
  next setup.

### Non-AWS storage providers

For S3-compatible endpoints (MinIO, R2, …) the remote additionally sets
`endpoint` and path-style addressing, matching how the platform talks to
them. Note the federation call (`AssumeRoleWithWebIdentity`) may need the
provider's own STS endpoint, which rclone cannot configure
(`--s3-sts-endpoint` is dead in the v2 SDK): if it fails, export
`AWS_ENDPOINT_URL_STS=<endpoint>` (or `AWS_ENDPOINT_URL`) in the mount
process's environment and retry. AWS S3 connections work without this.

### Mount etiquette

A mount makes bulk access tempting — the never-bulk-download rule still
applies to image data. Mounts shine for browsing a connection with normal
tools (open a results Parquet, read a sidecar, drag a config), where every
file is just a path.

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
  `~/.aws/cytario/<connection-name>/id_token`; rclone remotes
  `cytario-<connection-name>` (see "Mounting with rclone").
- If `auth token`/`connections list` returns "rejected", the grant was revoked
  or expired — ask the user to sign in again; do not retry in a loop.
