# clip-log-preflight

A small, read-only Python CLI for checking an episode clip log against local
camera/card directories before you act on the log.

Export five columns from a spreadsheet, supply one directory per camera/card
scope, and get either an exact filename manifest for every episode or a list of
blockers. It catches missing IDs, overlapping assignments and ambiguous
filenames. It does **not** download, copy, rename, inspect or edit media.

Python 3.10+; standard library only; no installation, account or network needed.
This is a standalone example, not an NLE plugin or a downloader.

## Quick start

Save a UTF-8 CSV with this exact header and column order:

```csv
episode,camera,card,first,last
E01,A,CARD01,A0012,A0015
E01,B,CARD01,B0045,B0048
E02,A,CARD02,A0012,A0013
```

Then run from this project's directory, replacing the paths with your own:

```sh
python3 clip_log_preflight.py examples/episode-log.csv \
  --source A CARD01 /path/to/camera-a/card-01 \
  --source B CARD01 /path/to/camera-b/card-01 \
  --source A CARD02 /path/to/camera-a/card-02
```

A source tree can contain nested folders. Each `--source` binds that entire tree
to one exact camera/card pair; folder names are never used to guess a scope.
Distinct cards can legitimately contain the same clip ID. Put them in distinct,
non-overlapping source trees and specify the card correctly in the CSV.

### Try it without real media

The following demonstration creates **empty filename fixtures**, runs the actual
CLI with the included synthetic CSV, then deletes its temporary directory. These
are not playable video files. The demo writes fixtures; the CLI only reads CSV
bytes and filesystem metadata.

```sh
python3 - <<'PY'
from pathlib import Path
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory(prefix="clip-log-demo-") as temporary:
    root = Path(temporary)
    command = [sys.executable, "clip_log_preflight.py", "examples/episode-log.csv"]
    for camera, card, first, last in [
        ("A", "CARD01", 12, 15),
        ("B", "CARD01", 45, 48),
        ("A", "CARD02", 12, 13),
    ]:
        directory = root / camera / card
        directory.mkdir(parents=True)
        for number in range(first, last + 1):
            (directory / f"{camera}{number:04d}.mov").touch()
        command.extend(["--source", camera, card, str(directory)])
    result = subprocess.run(command, check=False)
    raise SystemExit(result.returncode)
PY
```

Expected: `"status": "ready"`, two episode manifests, ten clip assignments and
exit code `0`. A ready result here demonstrates filename matching only. Temporary
paths in this particular demo stop existing when it finishes.

## Input rules

These are this tool's conventions, not assumptions about every camera or editing
workflow:

- Endpoints are **inclusive**: `A0012`–`A0015` means four IDs
- Only the final ASCII digit run is expanded. `CAM2_TAKE009`–`CAM2_TAKE010`
  preserves `CAM2_TAKE`, and `0012`–`0013` is also valid
- Both endpoints must have exactly the same prefix and digit width. `A001`–`A003`
  works; `A001`–`A03`, `A001`–`B003` and descending ranges do not
- Case, prefix and zero padding are preserved. `A001`, `a001`, `A01` and `001`
  are different stems. Unicode prefixes are allowed; suffix digits must be ASCII
  `0`–`9`. No Unicode normalization or fuzzy matching occurs
- `episode`, `camera`, `card`, `first` and `last` must all be nonempty and at most
  255 characters, with no surrounding whitespace, control characters or invalid
  Unicode. IDs must not contain `/` or `\`. A clip ID excludes its file extension
- Camera/card labels must match a configured source exactly, including case
- Each source scope is configured once. Duplicate scopes, overlapping root trees,
  and roots referring to the same directory are rejected
- One scoped clip can occur only once in an audit. Overlapping rows, repeated
  assignments to one episode, and reuse across episodes all block the audit
- Two requested paths referring to the same filesystem file also block, even
  under different IDs/scopes. This catches hardlink aliases using device/inode
  metadata without reading media bytes
- Intentional cross-episode reuse is possible in real work, but has no opt-in in
  this version. Review that reuse yourself and run the episodes as separate CSV
  audits if appropriate. Separate invocations do not check each other's history
- The header must be exactly `episode,camera,card,first,last`. Duplicate/reordered
  headers, additional/missing cells and blank records are rejected. Use comma CSV
  with conventional double-quote escaping; UTF-8 with an initial BOM is accepted

## What gets scanned

All configured sources are scanned recursively, including hidden directories and
hidden files. Only regular files with allowed suffixes become media candidates.
Unsupported regular files are counted and ignored; their content is never read.

Default extensions, compared case-insensitively:

```text
.ari .avi .braw .crm .m2ts .m4v .mkv .mov .mp4 .mpeg .mpg .mts .mxf .r3d .webm
```

`--extension` is repeatable and **replaces**, rather than adds to, the defaults:

```sh
python3 clip_log_preflight.py log.csv \
  --source A CARD01 /path/to/card \
  --extension mov --extension .mxf
```

The stem is the filename minus its final extension. No candidate filenames are
guessed. A logged ID must resolve to exactly one file in its camera/card scope:

- Zero matching files: `missing_clip`
- Two or more matching paths, even in separate subfolders or with different
  extensions: `ambiguous_clip`; all candidates are listed
- Exactly one: eligible for a manifest, if the entire audit passes

Unlogged files are not assigned to episodes. Duplicate stems that are never
referenced by the log are not themselves blockers.

### Fail-closed behavior and limits

Symlinks are never intentionally followed. A symlink in a source tree blocks the
audit even if it is broken, hidden or has an unsupported extension. A symlink in
a source root's or log file's path, including an ancestor component, also blocks.
Non-regular entries such as FIFOs and sockets block a source scan. Use ordinary
local directory trees, not link farms.

An unreadable directory, enumeration failure, invalid-Unicode filename, missing
path, missing file-identity metadata or exhausted cap prevents any ready manifest.
Input validation runs before source scanning; fix its blockers and rerun to
reveal any later filesystem problems.

Hard limits keep accidental range expansions and scans bounded:

| Limit | Maximum |
| --- | ---: |
| CSV bytes | 2 MiB |
| Data records | 10,000 |
| Source roots | 16 |
| Inclusive IDs per record | 10,000 |
| Expanded IDs across records, including duplicates | 100,000 |
| Scanned entries across all source trees, including directories/unsupported files | 100,000 |
| Reported blockers before stopping | 100 |

The diagnostic cap adds one final `diagnostic_limit` error. Split a genuinely
larger job into reviewed audits; do not interpret an incomplete scan as success.

## Output contract

Except for `--help`, stdout contains one JSON document. It has `schema_version: 1`,
`status` and an `errors` list. Error entries contain a stable `code`, a human
`message` and relevant context such as CSV `record`, scope or candidate paths.
`record` counts logical CSV records, with the header as record 1; parse errors can
also include `physical_line`.

Exit codes:

| Exit | Status | Meaning |
| ---: | --- | --- |
| `0` | `ready` | All assignments resolved uniquely; all episode manifests included |
| `1` | `blocked` | Log validation or matching blockers; no manifests |
| `2` | `error` | Malformed CSV, invalid configuration, I/O failure or a resource cap; no manifests |

A per-record oversized range is a validation blocker (`1`); aggregate resource
caps stop the audit (`2`). Missing/invalid CLI arguments are also JSON errors.
If stdout itself cannot be written, delivering a JSON error there is impossible;
the command returns `2` when it detects the failure.

On success, `sources` maps zero-based `source_index` values to camera/card labels
and absolute source directories. `manifests` contains an object for each episode,
with `clips` entries like:

```json
{
  "camera": "A",
  "card": "CARD01",
  "clip_id": "A0012",
  "record": 2,
  "relative_path": "day-01/A0012.mov",
  "source_index": 0
}
```

Relative paths use `/`. Output is deterministic for identical inputs and
filesystem state: no timestamps, sorted JSON keys, sorted episodes and clips.
All episodes succeed together or none receive a manifest. The `manifests` key is
**absent** on every blocked/error result, including when one episode matched.
Always check the exit code before using the output. Nothing consumes the manifest
automatically, and it is not an NLE interchange format.

You can redirect stdout to a report file using your shell. That shell operation
writes the chosen report; the CLI itself does not create output files. Reports
include local paths, filenames and labels, and OS errors may expose paths too.
Review and redact them before sharing publicly.

## Boundaries

- A correctly formatted but wrong ID that names an existing clip can pass
- This cannot detect corrupt/empty media, wrong takes, semantic B-roll matches,
  an incorrect camera/card choice, editorial intent or a correct finished edit
- Extension matching does not verify a file's media type. Zero-byte synthetic
  fixtures pass by design because this is a filename audit
- No media bytes are opened, so this does not calculate checksums or compare media
  contents. Hardlink aliases among requested files are rejected using filesystem
  identity, but separate copies of identical media are not detected. An unrequested
  hardlink alias does not by itself block the audit
- This is not a security boundary against concurrent filesystem changes. Keep
  source trees stable during the audit and before consuming a manifest. Metadata
  checks and directory traversal are not an atomic filesystem snapshot
- Reading the CSV or directory metadata may update access times (`atime`). The
  audit does not intentionally modify log/media bytes, names or modification times
- No network APIs, downloads, copying, renaming, deletion or external services.
  Use local storage: a path backed by an OS-mounted network filesystem can still
  cause filesystem traffic outside this program's control
- Local verification was on Linux with Python 3.12.14. The code targets Python
  3.10+; native Windows/macOS, other Python versions, NLE integration and real
  camera media have not been tested here

## Development and tests

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile clip_log_preflight.py
python3 clip_log_preflight.py --help
```

Tests create temporary synthetic filename fixtures (empty files and tiny
non-media sentinel content), never real video. They cover
range expansion, source collisions, exact matching, CSV/Unicode faults, scan
failures and caps, links, CLI JSON/exit behavior and unchanged input/media data.
Some OS-specific tests skip where the necessary filesystem feature is absent.

The included GitHub Actions workflow uses the runner's built-in `python3`, runs
these checks without package installs, has read-only contents permission and a
five-minute timeout. Its presence is not a claim that a remote CI run passed.

## License

Original implementation, tests and documentation are MIT licensed; see
[LICENSE](LICENSE). Synthetic example data is included for demonstration.
