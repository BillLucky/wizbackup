# Architecture and guarantees

[English](ARCHITECTURE.md) | [简体中文](ARCHITECTURE.zh-CN.md) · [README](../README.md)

## Design goal

Keep enough source material to inspect, verify, read, and convert a library after its original service becomes unavailable. A readable export is one useful view of an archive; it is not the sole preservation format.

## Components

```text
cli
 +-- account discovery
 |    +-- personal library
 |    +-- authorized team libraries
 +-- inventory + download
 |    +-- version pages
 |    +-- folder pages / cross-check
 |    +-- raw responses, resources, attachments
 +-- local interpretation
 |    +-- ZIWR decryption
 |    +-- verified attachment materialization
 |    +-- collaboration snapshot / HTML
 +-- reading pipeline
 |    +-- safe extraction
 |    +-- Markdown / offline HTML
 |    +-- optional external-resource mirror
 |    +-- local catalog / note links
 +-- verification + ZIP64
 +-- offline toolkit
      +-- inspect
      +-- restore to new directory
      +-- rebuild from retained objects
      +-- export separate vault
```

## Three data layers

| Layer | Contents | Role |
| --- | --- | --- |
| Source | Original responses, native objects, HTML, resource bytes, ciphertext, collaboration JSON | Evidence and input for future converters |
| Reading | Sanitized HTML, Markdown, materialized attachments, local paths | Human use and import into other applications |
| Control | Inventories, record state, identity/version, SHA-256, exceptions, reports | Completeness, resumption, recovery decisions |

Objects are scoped by library, note ID, and cloud version. Equal titles cannot overwrite one another. Team libraries have separate roots rather than being merged into the personal folder tree.

## Discovery is a correctness step

A version value is not a unique note identity. Advancing to the largest version plus one can cross a page boundary shared by multiple notes. The downloader independently enumerates folders, compares the two inventories, and unions results by note ID. Both scans must complete; a failed request is not interpreted as an empty library.

The archive includes available folder/tag metadata, including logical empty folders. Folder metadata remains the recovery source even when an extractor omits physical empty directories.

## Source bytes and reconstruction

When the content API returns a native URL, its downloaded bytes are retained. For ordinary notes, the modern API can return HTML and named resources instead. A package rebuilt from these parts is labeled reconstructed; it is not represented as a byte-identical historical server ZIP.

Collaboration documents require a structured snapshot and resource inventory in addition to HTML. The editor service's HTML export improves readability, while the snapshot preserves data for later interpretation.

Originals are retained throughout conversion. Local decryption creates a new ZIP. External-resource localization and internal-link rewriting change reading copies and their records, not the original cloud objects.

## Four different checks

```text
HTTP transfer complete?  -> did the response arrive without truncation?
Upstream checksum match?-> did supplied metadata agree with those bytes?
Local SHA-256 match?     -> did recorded local bytes remain unchanged?
Reading complete?       -> are body, attachments and references usable?
```

A ZIP CRC is a corruption check. A SHA-256 sidecar is a content fingerprint. Neither by itself authenticates an untrusted archive's author. A complete local object can still contain an unavailable external link.

For an upstream MD5 mismatch on an encrypted note, the recovery path requires the same identity/version, two identical ciphertext downloads, and successful certificate decryption with complete ZIP CRC verification. It preserves a source-checksum anomaly and a warning; it never relabels that MD5 as passed.

## State and resumption

A note record advances only after its files are written and hashed. Interrupted object transfers use staging files and restart; completed objects resume by version and SHA-256. Old attachment records lacking per-file verification are rebuilt before reuse.

Cloud deletions are archived as markers. The tool does not delete local history because an item disappeared remotely. Current records and reading copies describe the latest retained backup; historical object directories and dated ZIP snapshots must be kept for earlier recovery points.

## Scope boundaries

- Cloud content operations are read-only. Explicit local setup can use the login POST, and collaboration can issue an editor token with POST. Neither operation uploads note content or changes permissions. No content upload, remote deletion, or version revert is implemented.
- The cloud client targets allowed WizNote service domains; arbitrary self-hosted endpoints require a separate adapter/configuration.
- The personal classic ZIWR certificate format is supported. Enterprise certificate variants and multiple historical keys need separate adapters.
- A history inventory and current collaboration snapshot do not prove complete historical content.
- Permanently deleted material, inaccessible libraries, missing keys, and removed third-party resources cannot be inferred from metadata.
- Client SQLite/IndexedDB caches can provide independent evidence, but this release does not decode every client storage format.
- Offline recovery never contacts the cloud. It must preserve warnings from its source rather than manufacture missing data.
