# WizBackup

[English](README.md) | [简体中文](README.zh-CN.md)

WizNote (为知笔记) is a Chinese cloud note-taking application. WizBackup is an independent local backup, decryption, and migration tool.

Archive accessible WizNote personal and team libraries, keep the original data, and rebuild readable notes without the service.

**Using an Agent?** Give it this repository link and the [Agent quickstart](docs/AGENT_QUICKSTART.md), which explains skill installation and private local setup.

WizBackup is independent software for a one-way, local-first archive. It combines a cloud downloader, integrity manifests, offline HTML/Markdown conversion, local ZIWR decryption, and an offline recovery toolkit. Original data and reading copies remain separate.

**License:** [Apache-2.0](LICENSE). **Runtime:** Python 3.11+. **Platforms:** macOS and Linux. Windows is not currently supported by the CLI.

## Start with an offline rehearsal

You can try the recovery workflow using synthetic notes, without an account or a network connection after installation:

```sh
git clone https://github.com/BillLucky/wizbackup.git
cd wizbackup
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .

python examples/create_demo.py --output .demo
wizbackup inspect --source .demo/account/personal
wizbackup pack --output .demo/account/personal --archive .demo/personal.zip
wizbackup restore --source .demo/personal.zip --destination .demo/restored
wizbackup rebuild --source .demo/account/personal --destination .demo/rebuilt
wizbackup vault --source .demo/account/personal --destination .demo/vault
```

Choose a new destination for each rehearsal. Restore, rebuild, and vault export do not overwrite an existing non-empty destination. The demo generator creates synthetic content only; it is not a copy of someone's backup.

## What is preserved?

| Area | Archived representation | Boundary |
| --- | --- | --- |
| Personal and authorized team libraries | Separate inventory, object store, reading tree, and reports per library | Only libraries the current account can access |
| Folders, tags, note parameters, deletion markers | Original JSON metadata | Deletion markers do not recreate permanently deleted content |
| Ordinary notes | Original API response, HTML, resources, and an explicitly reconstructed ZIW | The modern API may provide components rather than a historical ZIP |
| Encrypted classic notes | Original ZIWR, encrypted certificate, locally decrypted ZIP, reading copies | Correct certificate password required; no brute force or security bypass |
| Collaboration notes | Structured snapshot, official HTML export, and resource inventory | Current snapshot; embedded application behavior may not survive static export |
| Attachments | Raw transfer bytes, verified materialized file, checksum evidence | Recognizes legacy MD5 and verified single-file transport ZIPs |
| Reading copies | Markdown, offline HTML, local resources, per-note metadata | Markdown cannot express every rich-document feature |
| History and comments | Available lists and responses; controlled per-note history retrieval | A history list is not proof that historical bodies were downloaded |
| External resources | Optional image/style mirror and explicit failures | Authentication, removed pages, network policy, or source errors can prevent retrieval |
| Recovery | Inspect, safe restore, reading rebuild, vault export | Offline operations do not restore a cloud account or its permissions |

The supported ZIWR workflow targets the classic personal certificate format. Separate enterprise certificates, multiple historical keys, and raw client IndexedDB decoding are not automatically supported.

## How the archive is built

```text
Authenticated WizNote account
  +-- personal library
  +-- accessible team / business libraries
                  |
                  v
  version inventory + independent folder inventory
                  |
                  v
  +---------------------+  +----------------------+
  | original objects    |  | original metadata    |
  | HTML / ZIW / ZIWR   |  | folders / tags / IDs |
  | images / attachments|  | dates / parameters   |
  +----------+----------+  +-----------+----------+
             |                         |
             +--- local conversion ----+
                         |
                         v
             Markdown + offline HTML
                         |
                         v
           records + SHA-256 verification
                         |
                         v
                ZIP64 + SHA-256 file
                         |
                         v
             inspect -> restore -> rebuild -> vault
                         (offline)
```

A changed cloud version receives a new object directory. Sync does not propagate cloud deletions into local file deletion. Reading copies are generated views; keep the originals for future converters.

## Keep software and private data apart

Use a separate private working directory. A source checkout should contain software, documentation, and synthetic fixtures only.

```text
workspace/
  wizbackup/                 public software checkout
  my-wiz-data/               private working directory
    .private/
      auth.json
      decryption-password.txt
    backups/
      account/
      archives/
```

### Guided local setup

From the activated environment, use a local interactive terminal:

```sh
wizbackup setup --data-dir ~/WizBackupData
```

Follow the local prompts and the returned next_command. Login passwords are not stored. If an Agent has no local TTY, enter credentials yourself in the terminal rather than in chat. See the [Agent workflow](docs/AGENT_QUICKSTART.md).

### Manual session configuration

The following alternative uses the sibling private directory shown above:

```sh
mkdir -m 700 ../my-wiz-data
cd ../my-wiz-data
umask 077
mkdir -m 700 .private
```

Create `.private/auth.json` in a text editor:

```json
{
  "token": "EXAMPLE_NOT_A_TOKEN",
  "kb_guid": "11111111-1111-4111-8111-111111111111",
  "kb_server": "https://kb.example.invalid"
}
```

Every value above is synthetic. Replace all fields with values from **your own authenticated WizNote session**. The reserved example host is intentionally unusable: use the actual HTTPS knowledge-base server returned by WizNote, rather than guessing a common server.

```sh
chmod 600 .private/auth.json
```

For encrypted notes, place the separate certificate password in `.private/decryption-password.txt` and set its mode to `600`. Use an editor or another private local input method; do not put a password in a command argument, shell history, issue, or screenshot.

The password is used locally. The tool preserves ciphertext and certificate data even when a note cannot be decrypted. A login password and a certificate password may differ.

## Back up an account

These relative-path examples assume you are inside the manually prepared private working directory. If you used guided setup, use its printed next_command with the configured paths.


```sh
# Discover the current account's accessible libraries.
wizbackup discover --auth .private/auth.json

# Archive personal and team libraries separately.
wizbackup backup-all --auth .private/auth.json --output backups/account --workers 4

# Test a small sample from the configured personal library.
wizbackup backup --auth .private/auth.json --output backups/sample --limit 2
```

A limited sample is never a complete archive. To resume, repeat the same full backup command. Completed notes are reused only when their version and local file hashes still match. Interrupted files restart atomically; this is object-level resumption, not HTTP byte-range resumption.

Expired credentials require an updated session token. The tool does not require disabling multi-factor authentication.

### Network selection

Both WizNote requests and external-resource requests default to **direct connections**, with environment proxy inheritance disabled.

```sh
# Explicit proxy for WizNote requests; external resources remain direct.
wizbackup backup-all --output backups/account --proxy http://127.0.0.1:7890

# Configure external-resource routing independently.
wizbackup backup-all --output backups/account --external-proxy http://127.0.0.1:7890

# Compatibility shortcut: a loopback proxy at port 7897.
wizbackup discover --network us
```

The label `us` does not test or guarantee a proxy's geographic exit location. Proxy URLs must not contain a username or password. Configuration affects this process, not the operating system.

To secure first-party objects before post-processing, use `--no-postprocess`. You can run `mirror-external` and `catalog` later. Third-party requests use a separate session without WizNote credentials, cookies, or a referring document URL. See [security boundaries](docs/SECURITY.md).

## Read and verify

```sh
wizbackup serve --output backups/account --port 8765
# Open http://127.0.0.1:8765 in a browser.

wizbackup verify --output backups/account/personal
wizbackup catalog --output backups/account/personal
wizbackup pack --output backups/account/personal --archive backups/archives/personal-snapshot.zip
```

The dashboard listens on loopback and displays counters only. It does not serve note files. To read your notes, open the library's local `index.html` or files in `readable/`.

A successful download, a valid archive, and a complete offline reading copy are separate claims. `verification.json` distinguishes local integrity from locked notes, missing resources, source-checksum anomalies, and reading warnings.

## Archive layout

```text
backups/account/
  knowledge-bases.json
  account-summary.json
  personal/
    metadata/                   inventories and source metadata
    objects/<note-id>/<version>/
      download.json             original content response
      original.ziw              native object, possibly ZIWR ciphertext
      decrypted.ziw             locally verified plaintext ZIP
      content.ziw               reconstructed ordinary-note package
      attachments/              original transfer bytes
      collaboration/            snapshot, HTML, resources
    readable/<folder>/<title--id>/
      note.md
      index.html
      metadata.json
      original/                 extracted source members
      attachments/              verified usable attachments
    external-resources/         optional shared local mirror
    records/<note-id>.json      relative paths, sizes, SHA-256, state
    catalog.json
    index.html
    verification.json
  teams/<knowledge-base-id>/    same format, isolated library
backups/archives/
  personal-snapshot.zip
  personal-snapshot.zip.sha256
```

Keep `readable/` **and its sibling `external-resources/`** together when exporting manually. The `vault` command copies the reading tree with the required sibling resources; it does not copy credentials or raw certificate data.

## Offline commands

| Command | Purpose |
| --- | --- |
| `inspect --source PATH` | Identify a library, ZIP, ZIW, or ZIWR and summarize its structure |
| `restore --source SNAPSHOT.zip --destination NEW_DIR` | Safely extract a new copy; validate ZIP and available checksum evidence |
| `rebuild --source LIBRARY --destination NEW_DIR` | Recreate reading copies from verified local objects; optional local decryption |
| `vault --source LIBRARY --destination NEW_DIR` | Create a separate reading workspace for Obsidian or other Markdown tools |
| `verify --output LIBRARY` | Check recorded files, identities, versions, and reading issues |
| `history --output LIBRARY --guid NOTE_ID` | Explicitly retry a note's historical bodies; this command is online |

Inspect is read-only. Restore, rebuild, and vault export write to a separate destination and never need a cloud token. Rebuild with encrypted originals may need `--password-file`. Start with [the recovery guide](docs/RESTORE.md) for a full drill.

## Exit codes and incomplete data

| Code | Meaning |
| ---: | --- |
| `0` | The command completed its documented scope |
| `1` | Invalid input, local I/O, authentication, network, or processing error |
| `2` | Incomplete backup or failed offline source check |
| `3` | Core archive exists, but reading/resource/source warnings remain |
| `4` | Catalog integrity precheck failed |
| `5` | Historical content retrieval is incomplete |

Read the command's JSON report, especially for offline recovery. A local SHA-256 match means the recorded bytes are intact; it does not certify that every old web resource or historical version still exists upstream. Service-side 500 responses, unavailable external images, and old metadata inconsistencies are retained as evidence, not rewritten as success.

## Documentation

| English | 简体中文 |
| --- | --- |
| [Agent quickstart and skill installation](docs/AGENT_QUICKSTART.md) | [Agent上手与技能安装](docs/AGENT_QUICKSTART.zh-CN.md) |
| [Architecture and guarantees](docs/ARCHITECTURE.md) | [架构与完整性口径](docs/ARCHITECTURE.zh-CN.md) |
| [Archive format](docs/FORMAT.md) | [档案格式](docs/FORMAT.zh-CN.md) |
| [Operations and troubleshooting](docs/OPERATIONS.md) | [运行与排错](docs/OPERATIONS.zh-CN.md) |
| [Recovery and migration](docs/RESTORE.md) | [恢复与迁移](docs/RESTORE.zh-CN.md) |
| [Privacy and security](docs/SECURITY.md) | [隐私与安全](docs/SECURITY.zh-CN.md) |
| [Protocol research and attribution](docs/RESEARCH.md) | [协议研究与归因](docs/RESEARCH.zh-CN.md) |

[Contributing](CONTRIBUTING.md) · [Security reporting](SECURITY.md) · [Changelog](CHANGELOG.md)

Tests use synthetic fixtures and mocked transport. CI must never require a real WizNote account:

```sh
python -m unittest discover -s tests -v
```

Apache-2.0 applies to the software. Archived notes, attachments, and upstream assets retain their own rights. WizNote and other product names belong to their respective owners.
