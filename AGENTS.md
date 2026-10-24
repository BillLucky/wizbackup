# Public repository rules

- This repository contains distributable software only. Never copy real notes, account identifiers, credentials, certificates, client databases, backup manifests, private reports, or an existing private Git history here.
- Use only synthetic fixtures in tests and examples. Networked tests must be opt-in and never run in CI with real accounts.
- Cloud content operations are read-only. Normal account sign-in and editor-token issuance are the only documented POST operations; neither may edit document content or permissions. Login passwords must never be saved or passed through agent chat.
- Preserve downloaded originals. Offline parsing and vault export must read their source and write to a separate destination. Never delete source material or silently treat a partial export as complete.
- Use portable configuration rather than a specific user's directory, proxy, or account. Keep environment proxy inheritance disabled unless explicitly selected.
- Document the difference between transport, local SHA-256, upstream checksums, readable exports, and unavailable resources.
- Keep English and Chinese documentation equivalent. License original code under Apache-2.0; retain attribution for referenced work.
- Run the relevant unittest suite, an offline recovery drill, a package-content check, and the publication privacy audit before release.
