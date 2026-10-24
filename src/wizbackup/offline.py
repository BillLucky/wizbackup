"""Offline inspection, safe restoration, regeneration, and portable vault export.

These functions never create a network client. The input is always read-only;
outputs are staged and published only into a new or empty destination. A
successful local checksum does not assert that upstream history or external
resources were complete.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata
from urllib.parse import quote, urlsplit, urlunsplit
import zipfile

from bs4 import BeautifulSoup

from .attachments import materialize_attachment
from .backup import note_path, safe_name, sha256, verify
from .catalog import finish_catalog
from .client import write_json
from .crypto import DecryptionError, decrypt_ziw
from .exporter import export_note, safe_filename

MAX_ENTRIES = 2_000_000
MAX_UNCOMPRESSED = 512 * 1024**3
MAX_ENTRY_BYTES = 16 * 1024**3
MAX_COMPRESSION_RATIO = 10_000
MAX_ENCRYPTED_BYTES = 512 * 1024**2
MAX_METADATA_BYTES = 64 * 1024**2
_CHUNK = 1024 * 1024
_HASH = re.compile(r"[0-9a-fA-F]{64}")


class OfflineError(ValueError):
    """An input cannot be safely interpreted or a checked output cannot be made."""


def _json(path: Path):
    try:
        if path.stat().st_size > MAX_METADATA_BYTES:
            raise OfflineError("JSON metadata exceeds the safe parsing limit")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise OfflineError("Invalid or unreadable JSON metadata") from None


def _relative(value) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise OfflineError("Unsafe relative path")
    parts = value.rstrip("/").split("/")
    if value.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise OfflineError("Unsafe relative path")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise OfflineError("Control character in path")
    return "/".join(parts)


def _file(root: Path, relative: str, *, require=True) -> Path:
    relative = _relative(relative)
    current = root
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise OfflineError("Symbolic links are not permitted")
    try:
        current.resolve().relative_to(root.resolve())
    except ValueError:
        raise OfflineError("Path escapes its source root") from None
    if require and (not current.is_file() or not stat.S_ISREG(current.stat().st_mode)):
        raise OfflineError("A required regular file is missing")
    return current


def _source(path: Path, *, directory=None) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise OfflineError("Source must not be a symbolic link")
    path = path.resolve()
    if not path.exists() or directory is True and not path.is_dir() or directory is False and not path.is_file():
        raise OfflineError("Source does not have the required file type")
    return path


@contextmanager
def _output(source: Path, destination: Path):
    destination = Path(destination)
    if destination.is_symlink():
        raise OfflineError("Destination must not be a symbolic link")
    destination = destination.resolve()
    if destination == source or source.is_dir() and (source in destination.parents or destination in source.parents):
        raise OfflineError("Source and destination must be separate trees")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise OfflineError("Destination must be new or empty")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".wizbackup-offline-", dir=destination.parent))
    stage.chmod(0o700)
    try:
        yield stage, destination
        # Only an explicitly selected, still-empty output directory may be
        # replaced. Source data and pre-existing nonempty outputs are untouched.
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
                raise OfflineError("Destination changed while output was being prepared")
            destination.rmdir()
        os.replace(stage, destination)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _copy(source: Path, destination: Path):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open("rb") as inp, destination.open("xb") as out:
        os.chmod(destination, 0o600)
        shutil.copyfileobj(inp, out, _CHUNK)
    if source.stat().st_size != destination.stat().st_size or sha256(source) != sha256(destination):
        raise OfflineError("Copy verification failed")


def _tree_files(root: Path):
    if root.is_symlink():
        raise OfflineError("Symbolic links are not permitted")
    if not root.is_dir():
        return
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in dirs + files:
            if (base / name).is_symlink():
                raise OfflineError("Symbolic links are not permitted")
        for name in files:
            path = base / name
            if not stat.S_ISREG(path.stat().st_mode):
                raise OfflineError("Special files are not permitted")
            yield path


def _library_roots(root: Path) -> list[Path]:
    """Discover library roots without traversing note resources as libraries."""
    result, queue = [], [(root, 0)]
    excluded = {"objects", "readable", "records", "metadata", "external-resources", ".private"}
    while queue:
        current, depth = queue.pop(0)
        if current.is_symlink():
            raise OfflineError("Symbolic links are not permitted")
        notes = current / "metadata/notes.json"
        if notes.is_file():
            _file(current, "metadata/notes.json")
            if (current / "records").is_symlink():
                raise OfflineError("Symbolic links are not permitted")
            result.append(current)
            continue
        if depth < 4:
            for child in sorted(current.iterdir()):
                if child.is_symlink():
                    raise OfflineError("Symbolic links are not permitted")
                if child.is_dir() and child.name not in excluded:
                    queue.append((child, depth + 1))
    return result


def _audit_library(root: Path):
    """Read-only counterpart of the registered-file portion of verify()."""
    notes = _json(_file(root, "metadata/notes.json"))
    if not isinstance(notes, list) or any(not isinstance(n, dict) or not n.get("docGuid") for n in notes):
        raise OfflineError("Invalid note inventory")
    inventory = {str(n["docGuid"]): n for n in notes}
    if len(inventory) != len(notes):
        raise OfflineError("Duplicate note identifiers in inventory")
    records, expected, problems, checked = {}, {}, Counter(), 0
    record_dir = root / "records"
    if record_dir.is_symlink() or not record_dir.is_dir():
        raise OfflineError("Record directory is missing or unsafe")
    for path in sorted(record_dir.glob("*.json")):
        path = _file(root, path.relative_to(root).as_posix())
        rec = _json(path)
        if not isinstance(rec, dict) or not isinstance(rec.get("guid"), str) or path.stem != rec["guid"]:
            raise OfflineError("Invalid record identity")
        guid = rec["guid"]
        if guid in records:
            raise OfflineError("Duplicate records")
        records[guid] = rec
        if guid in inventory and rec.get("version") != inventory[guid].get("version"):
            problems["stale-record"] += 1
        files = rec.get("files")
        if not isinstance(files, list) or not files:
            problems["unregistered-record"] += 1
            continue
        for item in files:
            if (not isinstance(item, dict) or not isinstance(item.get("size"), int)
                    or isinstance(item.get("size"), bool) or item["size"] < 0
                    or not _HASH.fullmatch(str(item.get("sha256", "")))):
                raise OfflineError("Invalid registered checksum")
            relative = _relative(item.get("path"))
            existing = expected.get(relative)
            if existing and (existing["size"] != item["size"] or existing["sha256"] != item["sha256"]):
                raise OfflineError("Conflicting registered checksums")
            expected[relative] = item
            try:
                file = _file(root, relative)
                if file.stat().st_size != item["size"] or sha256(file) != item["sha256"]:
                    problems["checksum-mismatch"] += 1
                if file.name == "metadata.json":
                    metadata = _json(file)
                    metadata = metadata.get("metadata", metadata) if isinstance(metadata, dict) else None
                    if not isinstance(metadata, dict):
                        raise OfflineError("Invalid document metadata")
                    identity = metadata.get("docGuid", metadata.get("documentGuid", metadata.get("guid")))
                    if identity is not None and identity != guid:
                        problems["metadata-identity-mismatch"] += 1
                    if metadata.get("version") is not None and metadata["version"] != rec.get("version"):
                        problems["metadata-version-mismatch"] += 1
                    if (inventory.get(guid, {}).get("kbGuid") and metadata.get("kbGuid") is not None
                            and metadata["kbGuid"] != inventory[guid]["kbGuid"]):
                        problems["metadata-library-mismatch"] += 1
            except OfflineError:
                problems["missing-or-unsafe-file"] += 1
            checked += 1
    problems["missing-record"] += len(set(inventory) - set(records))
    problems += Counter()  # Discard zero counts.
    fingerprint = hashlib.sha256()
    for relative, item in sorted(expected.items()):
        fingerprint.update(relative.encode() + b"\0" + item["sha256"].encode() + b"\n")
    result = {"notes": len(notes), "records": len(records), "files_checked": checked,
              "local_integrity_ok": not problems, "problems": dict(problems),
              "statuses": dict(Counter(r.get("status", "invalid") for r in records.values())),
              "source_checksum_anomalies": sum(bool(r.get("source_checksum_anomaly")) for r in records.values()),
              "readability_gap_notes": sum(bool(r.get("warnings") or any(r.get("export", {}).get(k)
                    for k in ("warnings", "missing_resources", "external_resources"))) for r in records.values()),
              "registered_files_fingerprint": fingerprint.hexdigest(),
              "integrity_scope": "registered file sizes and SHA-256; unregistered files not asserted"}
    return notes, records, expected, result


def _zip_plan(archive: zipfile.ZipFile):
    entries = archive.infolist()
    if len(entries) > MAX_ENTRIES or sum(i.file_size for i in entries) > MAX_UNCOMPRESSED:
        raise OfflineError("Archive exceeds the extraction limit")
    plan, seen, folded, files, dirs, duplicates = [], {}, {}, set(), set(), 0
    for entry in entries:
        name = _relative(entry.filename)
        if entry.flag_bits & 1:
            raise OfflineError("Password-encrypted ZIP transport is not supported")
        mode = (entry.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise OfflineError("Archive contains a link or special file")
        if entry.file_size > MAX_ENTRY_BYTES or entry.file_size > _CHUNK and entry.file_size / max(1, entry.compress_size) > MAX_COMPRESSION_RATIO:
            raise OfflineError("Archive member exceeds the extraction limit")
        canonical = unicodedata.normalize("NFC", name).casefold()
        if canonical in folded and folded[canonical] != name:
            raise OfflineError("Archive has a case or Unicode path collision")
        folded[canonical] = name
        parents = {str(p) for p in PurePosixPath(name).parents if str(p) != "."}
        if any(p in files for p in parents) or entry.is_dir() and name in files or not entry.is_dir() and name in dirs:
            raise OfflineError("Archive has a file/directory collision")
        dirs.update(parents)
        (dirs if entry.is_dir() else files).add(name)
        if name in seen:
            first = seen[name]
            if first.is_dir() != entry.is_dir():
                raise OfflineError("Archive has conflicting duplicate entries")
            if not entry.is_dir():
                if first.file_size != entry.file_size or first.CRC != entry.CRC:
                    raise OfflineError("Archive has conflicting duplicate entries")
                with archive.open(first) as left, archive.open(entry) as right:
                    while True:
                        a, b = left.read(_CHUNK), right.read(_CHUNK)
                        if a != b:
                            raise OfflineError("Archive has conflicting duplicate entries")
                        if not a:
                            break
            duplicates += 1
            continue
        seen[name] = entry
        plan.append((entry, name))
    return plan, duplicates


def _sidecar(source: Path, digest: str) -> bool:
    sidecar = source.with_suffix(source.suffix + ".sha256")
    if sidecar.is_symlink():
        raise OfflineError("Checksum sidecar must not be a symbolic link")
    if not sidecar.exists():
        return False
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise OfflineError("Expected one checksum sidecar entry")
    parts = lines[0].split(maxsplit=1)
    if not parts or not _HASH.fullmatch(parts[0]) or parts[0].lower() != digest:
        raise OfflineError("Archive SHA-256 sidecar mismatch")
    if len(parts) == 2 and parts[1].lstrip("*") != source.name:
        raise OfflineError("Checksum sidecar identifies another file")
    return True


def inspect_archive(source: Path) -> dict:
    """Inspect a library/account directory, backup ZIP, plain ZIW, or ZIWR.

    No title, note body, authentication field, or certificate value is returned.
    Directory integrity covers registered files. ZIP inspection checks structure
    and an optional SHA sidecar; full ZIP CRC is performed during restoration.
    """
    source = _source(source)
    if source.is_dir():
        libraries = _library_roots(source)
        if not libraries:
            raise OfflineError("No library inventory was found")
        summaries = []
        for root in libraries:
            _, _, _, audit = _audit_library(root)
            summaries.append({"relative_root": root.relative_to(source).as_posix(), **audit})
        return {"ok": all(r["local_integrity_ok"] for r in summaries),
                "status": "complete" if all(r["local_integrity_ok"] for r in summaries) else "partial",
                "format": "library" if libraries == [source] else "account",
                "encrypted": any(r["statuses"].get("locked", 0) for r in summaries),
                "libraries": summaries, "network_used": False}
    digest = sha256(source)
    with source.open("rb") as stream:
        magic = stream.read(4)
    if magic == b"ZIWR":
        return {"ok": source.stat().st_size >= 172, "status": "encrypted", "format": "ZIWR",
                "encrypted": True, "bytes": source.stat().st_size, "sha256": digest,
                "envelope_minimum_size_ok": source.stat().st_size >= 172,
                "content_checksums_verified": False, "network_used": False}
    if not zipfile.is_zipfile(source):
        return {"ok": False, "status": "unsupported", "format": "unknown",
                "encrypted": False, "bytes": source.stat().st_size, "sha256": digest, "network_used": False}
    verified_sidecar = _sidecar(source, digest)
    with zipfile.ZipFile(source) as archive:
        try:
            plan, duplicates = _zip_plan(archive)
        except OfflineError as error:
            return {"ok": False, "status": "unsafe", "format": "ZIP", "encrypted": False,
                    "sha256": digest, "safe_to_restore": False, "reason": str(error), "network_used": False}
        names = {name for _, name in plan}
        kind = "backup-ZIP" if any(n.endswith("metadata/notes.json") for n in names) else "ZIW" if "index.html" in names else "ZIP"
        return {"ok": True, "status": "inspected", "format": kind, "encrypted": False,
                "bytes": source.stat().st_size, "sha256": digest, "sha256_sidecar_verified": verified_sidecar,
                "entries": len(plan), "duplicate_identical_entries": duplicates,
                "uncompressed_bytes": sum(i.file_size for i, _ in plan),
                "safe_to_restore": True, "crc_checked": False, "network_used": False}


def restore_archive(source_zip: Path, destination: Path) -> dict:
    """Safely extract a ZIP/ZIP64 into a separate directory and verify libraries."""
    source = _source(source_zip, directory=False)
    before = sha256(source)
    sidecar = _sidecar(source, before)
    with _output(source, destination) as (stage, target):
        try:
            with zipfile.ZipFile(source) as archive:
                plan, duplicates = _zip_plan(archive)
                total = sum(entry.file_size for entry, _ in plan)
                if shutil.disk_usage(stage).free < total + min(1024**3, total // 20 + 1):
                    raise OfflineError("Insufficient free space for restoration")
                for entry, relative in plan:
                    path = stage / relative
                    if entry.is_dir():
                        path.mkdir(parents=True, exist_ok=True, mode=0o700)
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with archive.open(entry) as inp, path.open("xb") as out:
                        os.chmod(path, 0o600)
                        shutil.copyfileobj(inp, out, _CHUNK)
                    if path.stat().st_size != entry.file_size:
                        raise OfflineError("Restored member length differs from its declaration")
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
            raise OfflineError("Archive ZIP/CRC validation failed") from None
        if sha256(source) != before:
            raise OfflineError("Source archive changed during restoration")
        libraries = []
        for root in _library_roots(stage):
            # Reject unsafe record paths before verify() reads restored metadata.
            _, _, _, audit = _audit_library(root)
            reconstructed_directories = _category_directories(root)
            checked = verify(root)
            libraries.append({"relative_root": root.relative_to(stage).as_posix(),
                              "local_integrity_ok": checked["integrity_ok"],
                              "all_readable": checked["all_readable"],
                              "source_checksum_anomalies": checked.get("source_checksum_anomalies", 0),
                              "empty_logical_directories_recreated": reconstructed_directories,
                              "verification": checked, "audit": audit})
        integrity = all(r["local_integrity_ok"] for r in libraries)
        readable = all(r["all_readable"] for r in libraries) if libraries else None
        result = {"ok": integrity, "status": "complete" if integrity and readable is not False else "partial",
                  "operation": "restore", "network_used": False, "archive_sha256": before,
                  "sha256_sidecar_verified": sidecar, "archive_crc_ok": True,
                  "duplicate_identical_entries": duplicates, "local_integrity_ok": integrity,
                  "all_readable": readable, "libraries": libraries,
                  "integrity_scope": "archive CRC and registered library SHA-256" if libraries else "archive CRC only; no library manifests",
                  "report": "offline-restore-report.json"}
        write_json(stage / result["report"], result)
    return {**result, "destination": str(target)}


def _source_archive(root: Path, note: dict, expected: dict):
    guid, version = str(note["docGuid"]), str(note["version"])
    if not re.fullmatch(r"[A-Za-z0-9_-]+", guid) or not re.fullmatch(r"[0-9]+", version):
        raise OfflineError("Invalid note identity or version")
    prefix = "objects/" + guid + "/" + version + "/"
    candidates = ["decrypted.ziw", "collaboration/content.ziw", "content.ziw", "original.ziw"]
    if note.get("type") != "collaboration":
        candidates[1:3] = ["content.ziw", "collaboration/content.ziw"]
    for name in candidates:
        relative = prefix + name
        file = _file(root, relative, require=False)
        if file.is_file():
            if relative not in expected:
                raise OfflineError("Body has no registered checksum; inspect it separately")
            return file, prefix
    raise OfflineError("A registered source body is missing")


def _all_output_files(root: Path, *folders):
    return [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size, "sha256": sha256(p)}
            for folder in folders for p in sorted(_tree_files(folder) or [])]


def _category_directories(root: Path) -> int:
    """Recreate empty logical folders from the saved, non-executable metadata."""
    path = _file(root, "metadata/categories.json", require=False)
    if not path.is_file():
        return 0
    value = _json(path)
    values = value.get("result", []) if isinstance(value, dict) else value
    if not isinstance(values, list):
        return 0
    count = 0
    for category in values:
        if not isinstance(category, str):
            continue
        parts = [safe_name(part, 90) for part in category.split("/") if part]
        destination = root / "readable" / Path(*parts)
        if not destination.exists():
            destination.mkdir(parents=True, mode=0o700)
            count += 1
    return count


def _append_attachments(dest: Path, links: list[tuple[str, str]]):
    if not links:
        return
    md, markup = [], []
    for name, path in links:
        label = name.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
        md.append("- [" + label + "](" + path + ")")
        markup.append('<li><a href="' + html.escape(path, quote=True) + '">' + html.escape(name) + "</a></li>")
    markdown = dest / "note.md"
    if markdown.exists():
        markdown.write_text(markdown.read_text() + "\n\n## Attachments\n\n" + "\n".join(md) + "\n", encoding="utf-8")
    page = dest / "index.html"
    if page.exists():
        body = page.read_text()
        section = '<section class="backup-attachments"><h2>Attachments</h2><ul>' + "".join(markup) + "</ul></section>"
        page.write_text(body.replace("</body>", section + "</body>") if "</body>" in body else body + section, encoding="utf-8")


def _url_key(value):
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value.strip())
        if not parts.scheme and parts.netloc:
            parts = parts._replace(scheme="https")
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except ValueError:
        return None


def _cached_assets(source: Path, stage: Path, expected: dict):
    """Copy only registered, checksum-verified external assets; never fetch."""
    manifest = _file(source, "external-resources/manifest.json", require=False)
    if not manifest.is_file():
        return {}, {}
    value = _json(manifest)
    entries = value.get("resources") if isinstance(value, dict) else None
    if not isinstance(entries, dict):
        raise OfflineError("Invalid external cache manifest")
    mapping, accepted = {}, {}
    for url, entry in entries.items():
        key = _url_key(url)
        if key is None or not isinstance(entry, dict) or entry.get("status") != "downloaded":
            continue
        relative = _relative(entry.get("path"))
        path = PurePosixPath(relative)
        cache_key = hashlib.sha256(key.encode()).hexdigest()
        if (len(path.parts) != 2 or path.parts[0] != "external-resources"
                or not re.fullmatch(re.escape(cache_key) + r"\.[A-Za-z0-9]{1,10}", path.name)):
            raise OfflineError("Unsafe external cache asset path")
        if relative not in expected:
            continue
        registered = expected[relative]
        if entry.get("sha256") != registered["sha256"] or entry.get("size") != registered["size"]:
            raise OfflineError("Cache manifest conflicts with registered checksums")
        original = _file(source, relative)
        destination = stage / relative
        if not destination.exists():
            _copy(original, destination)
        for field in ("dependencies", "unresolved_dependencies"):
            if field in entry and (not isinstance(entry[field], list) or not all(isinstance(v, str) for v in entry[field])):
                raise OfflineError("Invalid cache dependency list")
        clean = {field: entry[field] for field in
                 ("status", "path", "size", "sha256", "content_type", "role", "dependencies", "unresolved_dependencies")
                 if field in entry}
        if entry.get("source_path"):
            original_css = _relative(entry["source_path"])
            if original_css != "external-resources/" + cache_key + ".source.css":
                raise OfflineError("Unsafe original CSS cache path")
            if original_css in expected:
                source_expected = expected[original_css]
                if entry.get("source_sha256") != source_expected["sha256"]:
                    raise OfflineError("Original CSS cache checksum conflicts with its record")
                _copy(_file(source, original_css), stage / original_css)
                clean.update(source_path=original_css, source_sha256=source_expected["sha256"])
        mapping[key], accepted[key] = relative, clean
    write_json(stage / "external-resources/manifest.json", {"resources": accepted, "offline_only": True})
    return mapping, accepted


_CSS_URL = re.compile(r"""url\(\s*(["']?)(.*?)\1\s*\)""", re.I)
_MD_LINK = re.compile(r'(!?\[[^\]\n]*\])\(([^()\s]+)(\s+"[^"\n]*")?\)')


def _localize_cached(dest: Path, stage: Path, exported: dict, mapping: dict, entries: dict,
                     original_export: dict):
    used, unresolved = set(), set()

    def mark(key):
        relative = mapping.get(key)
        if relative is None or relative in used:
            return
        used.add(relative)
        entry = entries[key]
        if entry.get("source_path"):
            used.add(entry["source_path"])
        for dependency in entry.get("dependencies", []):
            child = _url_key(dependency)
            if child in mapping:
                mark(child)
        unresolved.update(_url_key(url) or url for url in entry.get("unresolved_dependencies", []))

    def local(value):
        key = _url_key(value)
        if key not in mapping:
            return value
        mark(key)
        result = quote(os.path.relpath(stage / mapping[key], dest), safe="/")
        fragment = urlsplit(value).fragment
        return result + ("#" + quote(fragment, safe="-._~") if fragment else "")

    page = dest / "index.html"
    if page.is_file():
        soup = BeautifulSoup(page.read_text(encoding="utf-8"), "html.parser")
        for node in soup.find_all(True):
            for attr in ("src", "poster", "data-src", "data-original"):
                if isinstance(node.get(attr), str):
                    node[attr] = local(node[attr])
            if node.name == "link" and "stylesheet" in node.get("rel", []) and isinstance(node.get("href"), str):
                node["href"] = local(node["href"])
            if isinstance(node.get("style"), str):
                node["style"] = _CSS_URL.sub(lambda m: 'url("' + local(m[2]).replace('"', "%22") + '")', node["style"])
        for node in soup.find_all("style"):
            if node.string:
                node.string.replace_with(_CSS_URL.sub(lambda m: 'url("' + local(m[2]).replace('"', "%22") + '")', node.string))
        page.write_text(str(soup), encoding="utf-8")
    markdown = dest / "note.md"
    if markdown.is_file():
        body = markdown.read_text(encoding="utf-8")
        lines, fence, frontmatter = [], None, body.startswith("---\n")
        for index, line in enumerate(body.splitlines(keepends=True)):
            if frontmatter:
                lines.append(line)
                if index and line.strip() == "---":
                    frontmatter = False
                continue
            marker = re.match(r"^ {0,3}(" + chr(96) + r"{3,}|~{3,})", line)
            if marker:
                text = marker[1]
                fence = None if fence and text[0] == fence[0] and len(text) >= len(fence) else text
                lines.append(line)
                continue
            if fence or line.startswith(("    ", "\t")):
                lines.append(line)
                continue
            chunks = re.split("(" + chr(96) + "+.*?" + chr(96) + "+)", line)
            lines.append("".join(chunk if i % 2 else _MD_LINK.sub(
                lambda m: m[1] + "(" + local(m[2]) + (m[3] or "") + ")", chunk)
                for i, chunk in enumerate(chunks)))
        markdown.write_text("".join(lines), encoding="utf-8")
    candidates = [*exported.get("external_resources", []), *original_export.get("external_resources", [])]
    unresolved.update(_url_key(url) or url for url in candidates if _url_key(url) not in mapping)
    exported["external_resources"] = sorted(unresolved)
    exported["warnings"] = [w for w in exported.get("warnings", []) if "external resources were not fetched" not in w]
    if unresolved:
        exported["warnings"].append("Some external resources are unavailable in the verified local cache")
    exported["offline_cached_resources_used"] = len(used)
    return used


def rebuild_readable(source_root: Path, destination: Path, password_file: Path | None = None) -> dict:
    """Regenerate a single library without contacting Wiz or modifying its data.

    This produces a checked reading-oriented library, not a duplicate of every
    historical/control object. Keep the original backup and its certificates.
    Existing plain source archives are preferred; encrypted-only notes stay
    locked unless a local password and the original certificate are supplied.
    """
    source = _source(source_root, directory=True)
    notes, records, expected, audit = _audit_library(source)
    if not audit["local_integrity_ok"]:
        raise OfflineError("Source registered files failed integrity; originals were not changed")
    password = None
    if password_file is not None:
        password_path = _source(password_file, directory=False)
        if password_path.stat().st_mode & 0o077:
            raise OfflineError("Password file must have private permissions (600)")
        try:
            password = password_path.read_text(encoding="utf-8").rstrip("\r\n")
        except UnicodeError:
            raise OfflineError("Password file is not valid UTF-8") from None
    results = []
    with _output(source, destination) as (stage, target):
        write_json(stage / "metadata/notes.json", notes)
        cache_map, cache_entries = _cached_assets(source, stage, expected)
        for name in ("categories.json", "tags.json", "param.json", "attachment.json", "deleted.json"):
            original = _file(source, "metadata/" + name, require=False)
            if original.is_file():
                _copy(original, stage / "metadata" / name)
        for note in notes:
            guid = str(note["docGuid"])
            original_record = records[guid]
            archive, prefix = _source_archive(source, note, expected)
            obj = stage / prefix
            obj.mkdir(parents=True, exist_ok=True, mode=0o700)
            body = obj / "recovery-source.ziw"
            _copy(archive, body)
            warnings = list(original_record.get("warnings", []))
            with body.open("rb") as stream:
                encrypted = stream.read(4) == b"ZIWR"
            if encrypted and password is not None:
                try:
                    if body.stat().st_size > MAX_ENCRYPTED_BYTES:
                        raise DecryptionError("Encrypted original exceeds the safe in-memory limit")
                    cert = _json(_file(source, "metadata/user-cert.json"))
                    if not isinstance(cert, dict):
                        raise DecryptionError("Certificate metadata is not an object")
                    cert = cert.get("result", cert)
                    decoded = obj / "decrypted.ziw"
                    decrypt_ziw(body, decoded, cert, password)
                    body = decoded
                except (DecryptionError, OfflineError):
                    warnings.append("Local decryption failed; encrypted original retained")
            dest = stage / note_path(note)
            exported = export_note(body, dest, note)
            links, attachment_validations = [], []
            attachments_path = _file(source, prefix + "attachments.json", require=False)
            if attachments_path.is_file():
                if prefix + "attachments.json" not in expected:
                    raise OfflineError("Attachment metadata has no registered checksum")
                attachments = _json(attachments_path)
                if not isinstance(attachments, list):
                    raise OfflineError("Invalid attachment metadata")
                for att in attachments:
                    identity = str(att.get("attGuid", ""))
                    if not re.fullmatch(r"[A-Za-z0-9_-]+", identity):
                        raise OfflineError("Invalid attachment identifier")
                    relative = prefix + "attachments/" + identity + ".bin"
                    raw = _file(source, relative)
                    if relative not in expected:
                        raise OfflineError("Attachment has no registered checksum")
                    copied = obj / "attachments" / (identity + ".bin")
                    _copy(raw, copied)
                    name = safe_filename(str(att.get("name", identity)), max_bytes=160)
                    file = dest / "attachments" / (identity + "--" + name)
                    validated = materialize_attachment(copied, att, file)
                    attachment_validations.append({"attGuid": identity, **validated})
                    links.append((str(att.get("name", identity)), quote("attachments/" + file.name, safe="/")))
            elif int(note.get("attachmentCount", 0) or 0):
                warnings.append("Source attachment inventory is absent")
            _append_attachments(dest, links)
            cached_paths = _localize_cached(dest, stage, exported, cache_map, cache_entries,
                                           original_record.get("export", {}))
            reading_metadata = dest / "metadata.json"
            if reading_metadata.is_file():
                snapshot_metadata = _json(reading_metadata)
                snapshot_metadata["export"] = exported
                write_json(reading_metadata, snapshot_metadata)
            if exported.get("missing_resources"):
                warnings.append("Original source references missing local resources")
            record = {"guid": guid, "version": note["version"], "status": exported["status"],
                      "warnings": warnings, "readable_path": dest.relative_to(stage).as_posix(),
                      "export": exported, "attachments": len(attachment_validations),
                      "attachment_validations": attachment_validations,
                      "source_checksum_anomaly": original_record.get("source_checksum_anomaly"),
                      "source_metadata_anomalies": original_record.get("source_metadata_anomalies", []),
                      "files": _all_output_files(stage, obj, dest)}
            record["files"] += [{"path": relative, "size": (stage / relative).stat().st_size,
                                 "sha256": sha256(stage / relative)} for relative in sorted(cached_paths)]
            write_json(stage / "records" / (guid + ".json"), record)
            results.append({"guid": guid, "status": exported["status"],
                            "attachments": len(attachment_validations), "warnings": len(warnings)})
        password = None
        _category_directories(stage)
        catalog = finish_catalog(stage)
        if not catalog["ok"]:
            raise OfflineError("Generated reading catalog failed its integrity precheck")
        checked = verify(stage)
        # Detect concurrent modification while processing without writing a new
        # checksum over a changed source. All source hashes remain authoritative.
        _, _, _, after = _audit_library(source)
        if not after["local_integrity_ok"] or after["registered_files_fingerprint"] != audit["registered_files_fingerprint"]:
            raise OfflineError("Source changed during offline regeneration")
        result = {"ok": checked["integrity_ok"], "status": "complete" if checked["all_readable"] else "partial",
                  "operation": "rebuild", "network_used": False, "notes": len(notes),
                  "statuses": dict(Counter(r["status"] for r in results)),
                  "local_integrity_ok": checked["integrity_ok"], "all_readable": checked["all_readable"],
                  "source_registered_files_fingerprint": audit["registered_files_fingerprint"],
                  "source_checksum_anomalies": checked.get("source_checksum_anomalies", 0),
                  "verified_external_assets_available": len(cache_map),
                  "purpose": "reading regeneration; keep original backup for history, comments and certificates",
                  "catalog": catalog, "verification": checked, "notes_report": results,
                  "report": "offline-rebuild-report.json"}
        write_json(stage / result["report"], result)
    return {**result, "destination": str(target)}


def export_vault(source_root: Path, destination: Path) -> dict:
    """Copy verified reading files and their sibling assets to a new vault.

    Raw objects, API/control metadata, credentials, certificates and private
    client snapshots are not copied. Source reading notes may themselves contain
    personal information, so the resulting vault is still private user data.
    """
    source = _source(source_root, directory=True)
    notes, records, expected, audit = _audit_library(source)
    if not audit["local_integrity_ok"]:
        raise OfflineError("Source registered files failed integrity")
    selected = {}
    omitted_os_metadata = 0
    for folder in ("readable", "external-resources"):
        base = source / folder
        if not base.exists():
            continue
        for path in _tree_files(base):
            relative = path.relative_to(source).as_posix()
            if folder == "readable" and path.name in {".DS_Store", "Thumbs.db", "desktop.ini"}:
                omitted_os_metadata += 1
                continue
            if folder == "external-resources" and (relative not in expected or path.name.endswith(".source.css")):
                continue
            if relative not in expected:
                raise OfflineError("Reading file has no registered checksum")
            selected[relative] = path
    if not selected or not any(p.startswith("readable/") for p in selected):
        raise OfflineError("No registered reading files were found")
    with _output(source, destination) as (stage, target):
        entries = []
        for relative, path in sorted(selected.items()):
            _copy(path, stage / relative)
            entries.append({"path": relative, "size": path.stat().st_size, "sha256": expected[relative]["sha256"]})
        empty_directories = []
        for current, dirs, files in os.walk(source / "readable", followlinks=False):
            if not dirs and not files:
                relative = Path(current).relative_to(source).as_posix()
                _relative(relative)
                (stage / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
                empty_directories.append(relative)
        # Generate safe navigation rather than copying an unregistered source
        # HTML control file into the vault.
        rows = []
        for note in notes:
            rec = records[str(note["docGuid"])]
            relative = rec.get("readable_path")
            if relative and relative + "/index.html" in selected:
                rows.append('<li><a href="' + quote(relative + "/index.html", safe="/") + '">'
                            + html.escape(str(note.get("title", "Untitled"))) + "</a></li>")
        csp = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'"
        (stage / "index.html").write_text(
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="' + html.escape(csp, quote=True)
            + '"><title>Offline reading vault</title></head><body><h1>Offline reading vault</h1><ul>'
            + "".join(rows) + "</ul></body></html>", encoding="utf-8")
        (stage / "index.html").chmod(0o600)
        _, _, _, after = _audit_library(source)
        if not after["local_integrity_ok"] or after["registered_files_fingerprint"] != audit["registered_files_fingerprint"]:
            raise OfflineError("Source changed during vault export")
        gap_notes = audit["readability_gap_notes"] + sum(r.get("status") != "exported" for r in records.values())
        result = {"ok": True, "status": "partial" if gap_notes else "complete",
                  "operation": "vault", "network_used": False, "notes": audit["notes"],
                  "files_copied": len(entries), "local_integrity_ok": True,
                  "omitted_os_metadata": omitted_os_metadata,
                  "empty_directories_preserved": empty_directories,
                  "all_readable": not gap_notes, "readability_gap_notes": gap_notes,
                  "source_checksum_anomalies": audit["source_checksum_anomalies"],
                  "source_registered_files_fingerprint": audit["registered_files_fingerprint"],
                  "layout": "open this destination as a vault; keep readable and external-resources as siblings",
                  "files": entries, "report": "vault-manifest.json"}
        write_json(stage / result["report"], result)
    return {**result, "destination": str(target)}
