# Privacy and security boundaries

[English](SECURITY.md) | [简体中文](SECURITY.zh-CN.md) · [Report a vulnerability](../SECURITY.md)

## Data ownership and separation

The software checkout, a private working archive, and a shared synthetic example are different things. Do not publish real auth files, session captures, notes, certificates, client databases, inventories, or recovery reports.

An archive or vault can contain plaintext notes and private metadata. Output ZIPs are not encrypted by this project. A vault that omits credentials is still private content.

## Credentials and network

- The first-party client uses the configured allowed HTTPS knowledge-base host and the account service.
- Token headers are sent only to the configured knowledge-base host or account service, not to arbitrary resource hosts.
- Redirects are checked again. Object-storage requests do not receive the WizNote token header.
- Editor tokens remain local in memory and are not written to archive responses.
- The certificate password is used locally to decrypt; no password recovery or brute force is attempted.
- Proxy inheritance is disabled by default. Explicit first-party and external proxy settings are separate; the program does not modify the system network configuration.
- External-resource requests use clean sessions without WizNote headers, cookies, authorization, or a referring note URL.

Raw source responses may contain temporary signed object URLs. They belong in the private archive, not in issues or public fixtures. Reading metadata filters credential-like fields; this is not a promise to anonymize the original note body.

## Local HTTP service

The progress server binds to loopback. It validates request host, exposes a fixed counter schema, rejects arbitrary filesystem paths, and does not provide note downloads. The page uses local assets and a restrictive content security policy.

It is not intended for public hosting or remote administration. Use a controlled forwarding mechanism if remote access is needed, rather than changing the bind address casually.

## Untrusted archives and resources

Safe extraction checks paths, entry types, size, count, and conflicting names before publishing output. Symbolic links, devices, path traversal, and conflicting duplicate members are refused. A byte-identical duplicate member can be extracted once while preserving the source ZIP.

Reading HTML removes active content and blocks automatic external access with CSP. Read the generated view rather than opening an untrusted original page directly. The raw source is retained for evidence and future conversion.

External mirroring checks scheme, userinfo, port, DNS results, redirects, MIME/signatures, and resource limits. Cache files are restricted to expected URL-hash paths under the cache directory; a cache manifest cannot choose an arbitrary original-object path.

DNS validation is a local preflight. The HTTP connection does not pin the validated destination IP, and a proxy may resolve the hostname independently. The implementation therefore does not claim complete DNS-rebinding prevention. For untrusted link collections, retain first-party objects with `--no-postprocess` and review the optional mirroring scope separately.

## Safety limits

Limits are separate for a library snapshot and a single note package. They are guardrails, not an availability guarantee.

| Operation | Main limits |
| --- | --- |
| Whole-snapshot restore | 2,000,000 entries; 512 GiB uncompressed total; 16 GiB per entry |
| Single-note extraction | 20,000 entries; 2 GiB uncompressed total; 1 GiB per member; 64 MiB body conversion |
| Offline rebuild decryption | 512 MiB ciphertext auto-decryption cap; larger originals remain locked, retained plaintext can be used first |
| External mirror | 5,000 resources; 50 MiB per resource; 2 GiB downloaded total |
| CSS discovery | Depth 2; up to 200 references per stylesheet |

Compression-ratio and free-space checks also apply. Limits can cause an explicit partial/refused result; they must not silently truncate a supposedly complete archive. Read the code and release notes before changing limits for a trusted large corpus.

## Integrity is not authentication

ZIP CRC catches many accidental corruptions. Local SHA-256 checks that bytes match the recorded state. An upstream MD5 field may use a legacy representation or be inconsistent. None of these alone authenticates an archive supplied by an attacker.

A source-checksum anomaly must remain an anomaly even when repeated downloads and local decryption establish a recoverable object. Do not remove warnings by manually updating expected hashes.

## Write boundaries

- Cloud content and permissions are not modified.
- Explicit local setup may authenticate through a login POST. The editor-token POST obtains a credential for existing authorized access; subsequent content operations are GET. These authentication steps do not upload notes or change sharing permissions.
- Local originals and ciphertext are preserved.
- Offline restore/rebuild/vault operations use a separate destination.
- Verification writes its report; mirroring and catalog generation update reading copies and their records.
- No telemetry or automatic data upload is included.

For a security issue, use the [reporting policy](../SECURITY.md). Provide a synthetic reproducer rather than real notes or credentials.
