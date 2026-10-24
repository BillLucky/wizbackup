# Protocol research and attribution

[English](RESEARCH.md) | [简体中文](RESEARCH.zh-CN.md) · [README](../README.md)

These are interoperability notes, not a vendor API contract. The implementation separates protocol evidence, archive preservation, and readable conversion. Public source references do not guarantee that a current server supports every endpoint.

## Reading map

| Reference | Useful subject |
| --- | --- |
| [WizTeam/WizQTClient](https://github.com/WizTeam/WizQTClient) | Official client synchronization, object packaging, classic encrypted-note format |
| [famotime/Wiznotes_tools](https://github.com/famotime/Wiznotes_tools) | Community web-export workflow and format handling |
| [cloudy-liu/wiz-to-obsidian](https://github.com/cloudy-liu/wiz-to-obsidian) | Community remote/cached content and Markdown migration |
| [altairwei/WizNotePlus](https://github.com/altairwei/WizNotePlus) | Desktop client lineage and export use cases |
| [yoke88/wiznoteExporter](https://github.com/yoke88/wiznoteExporter) | Older archive/cache interpretation |

This is a source-reading map rather than a maintenance ranking or a recommendation to run unreviewed code. Referenced projects and assets retain their own licenses. WizBackup does not bundle the upstream desktop client.

## Inventory and ordinary content

The account service and knowledge-base service are separate. Use the knowledge-base server returned for the authenticated account, not a guessed common hostname.

| Data | Read endpoint |
| --- | --- |
| Folders | `GET /ks/category/all/{kb}` |
| Tags | `GET /ks/tag/all/{kb}` |
| Notes by version | `GET /ks/note/list/version/{kb}?version={cursor}&count={n}&pageSize={n}` |
| Notes by folder | `GET /ks/note/list/category/{kb}?category={path}&start={offset}&count={n}` |
| Attachment inventory | `GET /ks/attachment/list/version/{kb}` |
| Deletion markers | `GET /ks/deleted/list/version/{kb}` |
| Content and metadata | `GET /ks/note/download/{kb}/{doc}?downloadInfo=1&downloadData=1` |
| Note attachments | `GET /ks/note/attachments/{kb}/{doc}` |
| Attachment bytes | `GET /ks/object/download/{kb}/{doc}?objType=attachment&objId={att}` |
| Resource bytes | `GET /ks/object/download/{kb}/{doc}?objType=resource&objId={name}` |
| Personal certificate | `GET https://as.wiz.cn/as/user/cert` |

Primary references: [WizKMServer.cpp](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/sync/WizKMServer.cpp), [WizKMSync_p.h](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/sync/WizKMSync_p.h).

Several notes can share a version. A single maximum-version cursor is therefore insufficient evidence of completeness at a page boundary. The independent folder inventory provides a second check.

The ordinary response can contain `info`, `html` and `resources` at the top level. It must not be assumed that everything lives under `result`. The official modern client reconstructs a ZIP from HTML and resources when a native object URL is not supplied. WizBackup labels that reconstruction explicitly.

An HTTP 200 response is not sufficient object evidence. Legacy or missing-resource routes may return unrelated data or a placeholder. Match available metadata, type/signature, and archive checks; retain unresolved differences.

## Classic ZIWR v1

The header described by [WizZiwReader.h](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/share/WizZiwReader.h) is 156 bytes:

| Offset | Length | Field |
| ---: | ---: | --- |
| 0 | 4 | ASCII `ZIWR` |
| 4 | 4 | Little-endian version, supported value 1 |
| 8 | 4 | Little-endian encrypted-key length, supported value 128 |
| 12 | 128 | RSA-encrypted per-note password |
| 140 | 16 | Reserved bytes |
| 156 | Remaining | AES-encrypted ZIP |

The personal certificate contains RSA integers `n`, `e`, and a password-encrypted `d`. Compatibility requires the documented UTF-8/MD5-hex key derivation, fixed IV, CBC padding, and RSA PKCS#1 v1.5 operation before validating the resulting ZIP.

Primary references: [WizZiwReader.cpp](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/share/WizZiwReader.cpp), [WizEnc.cpp](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/share/WizEnc.cpp).

This is legacy-format decryption for interoperability, not a design for new encrypted storage. CRC is not authenticated encryption. Missing certificate passwords or unsupported key variants remain explicit failures/locked states.

## Attachment packaging and old MD5

[WizMd5.cpp](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/share/WizMd5.cpp) includes a representation that prints four little-endian DWORDs. This differs from the conventional hexadecimal byte order. [WizDatabase.cpp](https://github.com/WizTeam/WizQTClient/blob/7067bcfc421edce932aa3144ef2d1d631f27595a/src/share/WizDatabase.cpp) also documents compressed attachment transfer handling.

The adapter checks raw standard/legacy MD5 first. Only when a single safe ZIP member's checksum matches does it materialize the member as a transport wrapper. A genuine user ZIP matching the raw checksum is kept as a ZIP. Raw and materialized SHA-256 are recorded separately.

Stale size metadata and checksum mismatch are separate cases. A matching content checksum can justify recording a size anomaly. An unresolved upstream MD5 remains a warning even if stable encrypted bytes decrypt correctly.

## Collaboration

The published [wiz-editor package](https://www.npmjs.com/package/wiz-editor) provides additional protocol context. Content operations are read-only GET requests after editor-token issuance:

```text
POST /ks/note/{kb}/{doc}/tokens       issue a token for existing access
GET  /editor/{kb}/{doc}/versions/latest?metaOnly=false&cookie=false
GET  /editor/{kb}/{doc}/export/html?os=false
GET  /editor/{kb}/{doc}/resources
GET  /editor/{kb}/{doc}/resources/{name}
```

Snapshot, official HTML, and resource inventory are separate artifacts. The resource inventory can include assets not currently referenced by the rendered body. The published editor also describes other export endpoints; a source reference alone does not mean those exports are implemented by WizBackup.

Community [remote client code](https://github.com/cloudy-liu/wiz-to-obsidian/blob/2269d28312a6397cc11112f6ff297f24c21bf53e/src/wiz_to_obsidian/wiz_remote.py) documents another read path. The implementation uses the REST path and does not send editor operations or content updates.

## Historical content

```text
GET /ks/history/list/{kb}/{doc}?objType=document&objGuid={doc}
GET /ks/history/view/{kb}/{doc}?version={v}&editorGuid={editor}
```

Historical view parameters can include client-type fields. The read endpoint is distinct from the version-revert operation, which this project does not call.

Availability must be tested for the actual server and account. A list can succeed while body requests return a server error. Keep each version's downloaded/locked/failed/pending state, continuation indicators, and bounded-retry result. The current body is not a replacement for every historical version.

## Evidence discipline

- Retain source responses before conversion.
- Keep source identity/version and local bytes traceable.
- Distinguish source checksums from local SHA-256.
- Preserve failed and unsupported states.
- Use synthetic fixtures in public tests, examples, logs and issue reproducers.
- Attribute protocol sources without redistributing their code or assets under an unrelated license.
