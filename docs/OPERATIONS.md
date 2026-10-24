# Operations and troubleshooting

[English](OPERATIONS.md) | [简体中文](OPERATIONS.zh-CN.md) · [README](../README.md)

## Before an online run

1. Use a private working directory outside the software checkout.
2. Check available disk space. Raw objects, extracted files, reading copies, and a ZIP can coexist; plan for more than the compressed size.
3. Use credentials from your own session, with a mode-600 auth file.
4. Try a sample library export and one encrypted note if applicable.
5. Choose a new destination or resume the same known archive. Avoid overlapping backup/post-processing jobs.

The CLI's destination lock guards duplicate runs for that destination. Do not independently edit reading copies while a downloader, mirror, catalog, or pack operation is running.

Record the actual tool version (wizbackup --version) and source tag/commit for a run. A historic PID is not evidence of the current running version.

## Core workflow

```sh
wizbackup discover --auth .private/auth.json
wizbackup backup-all --auth .private/auth.json --output backups/account --workers 4
wizbackup verify --output backups/account/personal
wizbackup serve --output backups/account --port 8765
```

Four workers are a conservative starting point; the CLI caps workers at eight. The downloader backs off on rate limiting and server failures. A repeated command reuses verified completed notes, without deleting the archive first.

`--limit` is for a sample. `--no-refresh` uses an existing inventory and must not be used to prove that the remote library has no new items. If a discovery scan failed, complete a refreshed scan before claiming a full backup.

## Separate the stages

```sh
# Fetch accessible first-party objects without external post-processing.
wizbackup backup-all --output backups/account --no-postprocess

# Optional third-party images/styles, with explicit routing.
wizbackup mirror-external --output backups/account/personal --external-proxy direct

# Build a local index and convert known note links.
wizbackup catalog --output backups/account/personal

# Validate the current state, then create a new dated archive.
wizbackup verify --output backups/account/personal
wizbackup pack --output backups/account/personal --archive backups/archives/personal-snapshot.zip
```

`pack` refuses an existing output archive. Keep its `.sha256` sidecar with the ZIP. It creates a snapshot; it does not resolve missing resources.

## Network configuration

| Setting | Scope |
| --- | --- |
| Default `direct` | First-party cloud requests bypass inherited proxy environment variables |
| `--network us` | Compatibility alias for `http://127.0.0.1:7897`; no geographic verification |
| `--proxy http://127.0.0.1:7890` | Explicit per-process first-party HTTP(S) proxy |
| `--external-proxy direct` | Explicit direct external-resource routing |
| `--external-proxy http://127.0.0.1:7890` | Separate proxy for external images/styles |

No command changes operating-system proxy settings. A failed selected proxy is an error, not permission to fall back silently to another route. Proxy credentials in URLs are rejected.

## Progress

The loopback dashboard shows each library separately, note totals and states, external-resource progress, and verification summaries. It never lists or serves note content.

```text
discovery -> inventory -> cross-check -> download
                                     -> optional mirror -> catalog
                                                        -> verify -> package
```

A note counter reaching 100% can precede mirroring or compression. Read the phase label as well as the counter. Operational progress is not a replacement for `verification.json`.

## Background operation

Run from an activated environment in a persistent terminal or a task manager you control. Verify the actual process and a changing progress timestamp after starting it. Do not assume that a shell's successful background-launch message proves the process survived.

Project helper scripts are local conveniences, not an operating-system service or a promise of automatic restart. After reboot, rerun the documented command; completed objects are checked before reuse. Keep logs in the private working directory.

## Failure decisions

| Symptom | Action |
| --- | --- |
| Authentication expired / access denied | Refresh your normal session and update the private auth file; preserve the existing archive |
| Incorrect certificate password | Confirm the separate local password; keep ciphertext and certificate |
| Folder/version inventory disagrees | Retain both inventories and finish cross-checking; do not guess completeness from UI counts |
| HTTP 429 or temporary 5xx | Allow bounded backoff; inspect reports before retrying a smaller scope |
| History listing works but a body fails | Retain the list and per-version status; retry explicitly later |
| External image fails | Keep original HTML/reference and the failure record; do not replace it with a generated image |
| Upstream MD5 differs | Read the source anomaly record; local SHA and ZIP CRC are distinct evidence |
| Local SHA differs | Preserve the report and source; repair from a verified object or another known snapshot |
| ZIP extraction refused | Inspect the recorded path/type/size issue; do not disable safeguards for an unknown archive |
| Vault images disappear after a manual copy | Copy sibling external-resources as well as readable, or use vault export |

An HTTP 200 response can still be a login/error page or a placeholder. Content checks and source evidence matter.

## Exit codes

| Code | Interpretation |
| ---: | --- |
| 0 | Command completed its declared scope |
| 1 | Input/I/O/authentication/network/processing error |
| 2 | Missing current notes or failed offline source check |
| 3 | Core archive retained with reading, resource or source warnings |
| 4 | Catalog integrity precheck failed |
| 5 | Historical content retrieval is incomplete |

Offline commands report completeness, source integrity, and inherited readability gaps separately. Always retain their JSON result with the recovery work. Current-note success does not certify historical-body availability.

## A post-processing interruption

Reading files and their record are separate filesystem objects. If interrupted between updating a reading file and committing its record, a later check can report `RecordIntegrityFailed`. The tool refuses to bless those changed bytes as the old valid record.

Preserve the original archive. If online access remains available, rerun the backup to regenerate that note. For offline work, use `rebuild` into a fresh destination; verified original objects remain the recovery input. Do not edit expected hashes manually to remove the warning.

## Historical bodies

```sh
wizbackup history --output backups/account/personal --guid 33333333-3333-4333-8333-333333333333
```

The ID is synthetic; use a note from your local inventory. The command uses the authenticated cloud session and the documented read endpoint, not the version-revert endpoint. Consecutive server errors open a per-note circuit breaker; remaining versions stay pending. Server availability can change, so keep failures retryable instead of treating them as an empty history.

## Incremental use

Run the same full account command again to discover current accessible libraries and versions. Retain dated snapshots for older recovery points. Edit an independent vault rather than the archive's generated reading layer, so future verification has an intact baseline.
