# Archive format v1

[English](FORMAT.md) | [简体中文](FORMAT.zh-CN.md) · [README](../README.md)

## Identity and paths

A note is identified by its library ID, note ID, and cloud version. Titles are display names. Original titles and folder paths remain in metadata even when the filesystem representation requires a safe filename.

`files[].path` and `readable_path` are relative to a library root. These are the authoritative recovery paths. A converter's diagnostic path may describe its generation environment; recovery tools must not rely on it.

## Files

| Path | Meaning |
| --- | --- |
| `metadata/notes.json` | Current note inventory: union of completed version and folder scans |
| `metadata/inventory-crosscheck.json` | Counts and differences between independent inventories |
| `metadata/categories.json` | Folder response, including logical empty folders |
| `metadata/tags.json` | Tag names, identities and hierarchy |
| `metadata/attachment.json` | Library attachment metadata |
| `metadata/param.json` | Note parameters |
| `metadata/deleted.json` | Available deletion markers; no local-delete instruction |
| `metadata/user-cert.json` | Encrypted certificate data, not a plaintext certificate password |
| `objects/<id>/<version>/download.json` | Original content response |
| `objects/.../metadata.json` | Original note metadata |
| `objects/.../original.ziw` | Native transfer object; the magic determines whether it is ZIW or ZIWR |
| `objects/.../decrypted.ziw` | Locally decrypted ZIP that passed content/CRC checks |
| `objects/.../content.ziw` | Package reconstructed from HTML and resources |
| `objects/.../resources/` | Downloaded ordinary-note resource bytes |
| `objects/.../attachments/` | Original attachment transfer bytes |
| `objects/.../collaboration/` | Snapshot JSON, official HTML, resources, reconstructed package |
| `objects/.../comments.json` | Available comment response |
| `objects/.../history.json` | Available history inventory; not historical bodies |
| `readable/` | Generated Markdown, offline HTML, metadata, extracted members, attachments |
| `external-resources/` | Optional mirror, source CSS, cache manifest and failures |
| `records/<id>.json` | Version, state, warnings and per-file size/SHA-256 |
| `catalog.json` and `index.html` | Generated note lookup and local navigation |
| `verification.json` | Result of the most recent local integrity and reading checks |
| `progress.json` | Operational counters, not a completeness certificate |

Account-level `knowledge-bases.json` maps isolated library roots. `account-summary.json` connects per-library backup, verification, post-processing and archive results.

## Synthetic record excerpt

The following is an explanatory excerpt, not a full valid record or real account:

```json
{
  "guid": "33333333-3333-4333-8333-333333333333",
  "version": 7,
  "status": "exported",
  "readable_path": "readable/Example/Example--33333333-3333-4333-8333-333333333333",
  "files": [
    {
      "path": "objects/33333333-3333-4333-8333-333333333333/7/index.html",
      "size": 123,
      "sha256": "EXAMPLE_SHA256_REPLACE_WITH_64_HEX_DIGITS"
    }
  ],
  "warnings": []
}
```

Record states include `exported`, `locked`, `unsupported`, `failed` and `auth_required`. An exported body can still have resource warnings. A locked original can be preserved without being readable.

## Attachment validation

A transfer may be an original file or a legacy single-file ZIP wrapper. Match raw checksums first so a user's ZIP stays a ZIP.

| Validation mode | Interpretation |
| --- | --- |
| `raw-md5` | Raw bytes match standard MD5 |
| `raw-legacy-md5` | Raw bytes match Wiz's old DWORD-formatted MD5 |
| `wrapped-member-md5` | The sole safe ZIP member matches standard MD5 |
| `wrapped-member-legacy-md5` | The sole safe member matches the old representation |

The adapter retains raw and materialized SHA-256 separately. A successful checksum with stale size metadata is recorded as a size anomaly, not silently discarded.

## Preserved exceptions

- `source_metadata_anomalies` records supported interpretations of inconsistent metadata, including stable CSS responses or malformed navigation.
- `source_checksum_anomaly` means an upstream checksum still did not match. A stable encrypted source can be recovered only with the documented repeated-download and decryption/CRC checks; the checksum remains failed.
- `export.missing_resources` lists unresolved local references.
- `export.external_resources` lists unresolved external references.
- `ignored_client_dependencies` records precisely identified unused client UI dependencies removed only from reading copies.
- Identical duplicate ZIP members may be extracted once and counted. Conflicting duplicates, unsafe paths and special files are refused.
- Unknown metadata should remain available in the original JSON for future adapters.

## Relocation and snapshots

Changing the parent directory or library directory name does not change authoritative relative paths. Keep `readable/` with sibling `external-resources/`. A vault export preserves that relationship.

Older object directories are retained when a cloud version changes. Current records describe the latest retained version; retain dated ZIP snapshots for stable recovery points. SHA-256 sidecars identify a ZIP's bytes but do not provide a digital signature or encryption.
