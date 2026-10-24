# Offline recovery and migration

[English](RESTORE.md) | [简体中文](RESTORE.zh-CN.md) · [README](../README.md)

## Recovery ladder

```text
Need a quick inventory?       inspect
Have a complete ZIP?          restore into a new directory
Reading files need repair?   rebuild from verified retained objects
Want an editable workspace?  vault into a separate directory
Need old cloud revisions?    history (online, explicitly requested)
```

Use an activated environment or an absolute path to its `wizbackup` executable. Install dependencies before disconnecting. The four offline commands do not require an auth token.

## 1. Inspect the source

```sh
wizbackup inspect --source backups/account/personal
wizbackup inspect --source backups/archives/personal-snapshot.zip
```

Inspect distinguishes a library or account snapshot, ordinary ZIW ZIP, and ZIWR ciphertext. It reports what can be recognized; encrypted bytes are not a successfully converted body.

Keep inspection output with the recovery work. It can describe private identifiers or structure, so do not publish real reports.

## 2. Verify the ZIP fingerprint

Keep the ZIP and its sibling `.sha256` file together:

```sh
cd backups/archives
shasum -a 256 -c personal-snapshot.zip.sha256
```

The sidecar contains a relative filename, so run the check from the ZIP directory. Linux can use `sha256sum -c`. A missing sidecar means you lack that fingerprint comparison; it is not evidence that the ZIP is valid or invalid.

## 3. Restore to a new directory

```sh
wizbackup restore --source personal-snapshot.zip --destination ../../recovery-restored
```

The archive's top-level directory is retained. For a library ZIP created from `personal/`, a typical restored library root is `recovery-restored/personal/`. Read the returned library paths instead of assuming every archive uses that name.

```sh
wizbackup verify --output ../../recovery-restored/personal
```

Restore rejects unsafe paths, special files, conflicting archive entries, exceeded safety limits, and a non-empty destination. It validates ZIP structure/CRC and available checksum evidence, then reports discovered libraries and local verification results. It does not upload or merge content into an existing knowledge base.

## 4. Rebuild reading copies

```sh
wizbackup rebuild --source ../../recovery-restored/personal --destination ../../recovery-rebuilt
```

Rebuild reads a single library root containing `metadata/notes.json` and the retained object/record structure. It verifies source evidence, then uses original HTML/resource packages, decrypted ZIW, or collaboration material to produce a new reading tree.

The rebuilt output is a reading-oriented library, not a replacement for the full source archive. It does not promise to copy every original, comment, historical response, or certificate. Keep the source archive separately.

If only ciphertext is available and the matching supported certificate is retained:

```sh
wizbackup rebuild --source ../../recovery-restored/personal \
  --destination ../../recovery-rebuilt-with-decryption \
  --password-file ../../.private/decryption-password.txt
```

Store the password file privately with mode `600`. The source is not edited. A missing password or unavailable resource stays visible as a locked/partial result. Rebuild cannot invent historical data or fix an absent original.

## 5. Export a reading vault

```sh
wizbackup vault --source ../../recovery-restored/personal --destination ../../reading-vault
```

The exported workspace keeps the reading tree and sibling mirrored resources:

```text
reading-vault/
  readable/
    Example/
      Example--<note-id>/
        note.md
        index.html
        metadata.json
        original/
        attachments/
  external-resources/
```

Open the **vault root**, not just its `readable/` contents, in a Markdown application. Relative paths between sibling directories must remain intact.

Vault export leaves the archive unchanged and omits the raw object/certificate/credential storage. It still contains private note text and metadata; it is not an anonymized publication export.

## A useful recovery drill

1. Use the synthetic demo first.
2. Copy a real ZIP and its fingerprint to a separate location under your control.
3. Restore into a new path with a different parent/library directory name.
4. Verify file sizes and hashes.
5. Disconnect the network and open sample Markdown/HTML notes.
6. Include a note with images, an attachment, a decrypted source, and a collaboration source when those types exist.
7. Check actual attachment links and note links, not only file existence.
8. Record missing assets, unsupported material, source anomalies, and the exact tool version.

A synthetic drill proves the recovery code path. It does not prove that a particular private archive is complete; that requires inspecting its own reports and files.

## Fidelity and cloud restoration

Markdown may simplify tables, handwriting, embedded tools, and rich collaboration layouts. Original HTML, JSON snapshots, attachments, and resources are the recovery sources for better future converters.

Cloud-account recreation, access permissions, server upload, and deletion recovery are separate operations. This project has no cloud write-back adapter.
