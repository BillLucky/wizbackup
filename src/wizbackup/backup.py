from __future__ import annotations

import concurrent.futures
import hashlib
import html
import json
import os
import re
import shutil
import time
import zipfile
import uuid
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .client import WizClient, ApiError, AuthError, atomic_bytes, write_json


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_name(text, max_bytes=110):
    text = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", str(text)).strip().rstrip(". ")
    if text in ("", ".", ".."): text = "Untitled"
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def safe_resource(name):
    name = str(name).replace("\\", "/")
    p = PurePosixPath(name)
    if p.is_absolute() or any(x in ("", ".", "..") for x in p.parts) or ":" in name or "\x00" in name:
        raise ApiError("Unsafe resource name")
    return str(p)


def note_path(note):
    parts = [safe_name(x, 90) for x in note.get("category", "/").split("/") if x]
    return Path("readable", *parts, safe_name(note.get("title", "Untitled")) + "--" + note["docGuid"])


def inventory(client, root):
    meta = root / "metadata"
    meta.mkdir(parents=True, exist_ok=True)
    for name, path in [("categories", f"/ks/category/all/{client.kb}"), ("tags", f"/ks/tag/all/{client.kb}")]:
        write_json(meta / f"{name}.json", client.api(path))
    notes = []
    for page in client.version_list("note"):
        notes.extend(page)
        write_json(root / "progress.json", {"phase": "inventory", "discovered": len(notes), "updated": time.time()})
    if len({r["docGuid"] for r in notes}) != len(notes):
        raise ApiError("Duplicate note IDs in inventory")
    write_json(meta / "notes.json", notes)
    # Versions are not unique: a page boundary can split several notes sharing
    # one version. Independently enumerate EVERY category, then union by GUID.
    by_guid = {n["docGuid"]: n for n in notes}
    categories = json.loads((meta / "categories.json").read_text()).get("result", [])
    folders = sorted(set(["/"] + [c for c in categories if isinstance(c, str)] + [n.get("category", "/") for n in notes]))
    checked = 0
    category_guids = set()
    for category in folders:
        start, prior = 0, set()
        while True:
            data = client.api(f"/ks/note/list/category/{client.kb}", category=category,
                              start=start, count=100, withAbstract="false")
            page = data.get("result")
            if not isinstance(page, list): raise ApiError("Invalid category inventory")
            if not page: break
            ids = tuple(x["docGuid"] for x in page)
            if ids in prior: raise ApiError("Category pagination repeated")
            prior.add(ids)
            for row in page:
                category_guids.add(row["docGuid"])
                by_guid[row["docGuid"]] = row
            start += len(page)
        checked += 1
        # Preserve empty categories in the reading tree too.
        (root / "readable" / Path(*[safe_name(x, 90) for x in category.split("/") if x])).mkdir(parents=True, exist_ok=True)
        if checked % 25 == 0:
            write_json(root / "progress.json", {"phase": "inventory-crosscheck", "categories_checked": checked,
                       "categories_total": len(folders), "discovered": len(by_guid), "updated": time.time()})
    write_json(meta / "inventory-crosscheck.json", {"version_count": len(notes), "category_count": len(category_guids),
               "category_only": sorted(category_guids - {n["docGuid"] for n in notes}),
               "version_only": sorted({n["docGuid"] for n in notes} - category_guids), "categories_checked": checked})
    notes = list(by_guid.values())
    write_json(meta / "notes.json", notes)
    for kind in ("attachment", "param", "deleted"):
        rows = [row for page in client.version_list(kind) for row in page]
        write_json(meta / f"{kind}.json", rows)
    cert = client.api("https://as.wiz.cn/as/user/cert")
    write_json(meta / "user-cert.json", cert)
    write_json(meta / "inventory-summary.json", {
        "notes": len(notes), "categories": len(json.loads((meta / "categories.json").read_text()).get("result", [])),
        "protected": sum(bool(x.get("protected")) for x in notes),
        "body_bytes_reported": sum(x.get("dataSize", 0) for x in notes),
        "attachments_reported": sum(x.get("attachmentCount", 0) for x in notes),
        "file_types": dict(Counter(x.get("fileType", "") for x in notes)), "time": time.time(),
    })
    return notes


def valid_record(root, record, note):
    if record.get("version") != note["version"] or record.get("status") not in ("exported", "locked") or record.get("warnings"):
        return False
    attachments = record.get("attachments", 0)
    validations = record.get("attachment_validations")
    if attachments > 0 and (not isinstance(validations, list) or len(validations) != attachments):
        return False
    for f in record.get("files", []):
        p = root / f["path"]
        if not p.is_file() or p.stat().st_size != f["size"] or sha256(p) != f["sha256"]:
            return False
    return bool(record.get("files"))


def process_note(auth, network, root, note, *, supplemental=True, password_file=None):
    from .exporter import export_note
    client = WizClient(auth, network)
    guid = note["docGuid"]
    try:
        uuid.UUID(guid)
        if isinstance(note["version"], bool) or int(note["version"]) < 0: raise ValueError
    except (ValueError, TypeError, AttributeError):
        client.session.close()
        raise ApiError("Invalid document identity or version") from None
    obj = root / "objects" / guid / str(note["version"])
    record_path = root / "records" / f"{guid}.json"
    if record_path.exists():
        old = json.loads(record_path.read_text())
        can_decrypt = password_file and Path(password_file).exists()
        if valid_record(root, old, note) and not (old.get("status") == "locked" and can_decrypt):
            client.session.close()
            return old, True
    obj.mkdir(parents=True, exist_ok=True)
    rec = {"guid": guid, "version": note["version"], "status": "pending", "warnings": [], "files": []}
    try:
        payload = client.api(f"/ks/note/download/{client.kb}/{guid}", downloadInfo=1, downloadData=1)
        write_json(obj / "download.json", payload)
        info = payload.get("info", note)
        if info.get("docGuid") != guid or info.get("kbGuid") != client.kb or int(info.get("version", -1)) != int(note["version"]):
            raise ApiError("Downloaded metadata identity/version differs from inventory; refresh required")
        write_json(obj / "metadata.json", info)
        resources = payload.get("resources", []) or []
        native = obj / "original.ziw"
        archive = obj / "content.ziw"
        if payload.get("url"):
            # If the previous record failed integrity, do not bless a corrupt
            # existing object by simply calculating a new checksum for it.
            client.download(payload["url"], native)
            from .attachments import legacy_md5
            source_object_verified = False
            decrypted_for_recovery = False
            def decrypt_current():
                from .crypto import decrypt_ziw
                cert = json.loads((root / "metadata/user-cert.json").read_text())["result"]
                password = Path(password_file).read_text().rstrip("\r\n")
                try: decrypt_ziw(native, obj / "decrypted.ziw", cert, password)
                finally: password = None
            if info.get("dataMd5"):
                with native.open("rb") as source:
                    digest = hashlib.file_digest(source, "md5").digest()
                expected = info["dataMd5"].lower()
                if expected not in (digest.hex(), legacy_md5(digest)):
                    if not (info.get("protected") and password_file and Path(password_file).exists()):
                        raise ApiError("Original body MD5 mismatch")
                    # A few old encrypted notes have inconsistent upstream MD5
                    # metadata. Do not pretend the checksum passed: preserve two
                    # stable server responses and recover only when the user's
                    # certificate decrypts a ZIP whose complete CRC is valid.
                    second = client.api(f"/ks/note/download/{client.kb}/{guid}", downloadInfo=1, downloadData=1)
                    second_info = second.get("info", {})
                    if (second_info.get("docGuid") != guid or second_info.get("kbGuid") != client.kb
                            or second_info.get("version") != info["version"] or not second.get("url")):
                        raise ApiError("Body identity changed during checksum anomaly recovery")
                    write_json(obj / "checksum-recheck.json", second)
                    checked = obj / "checksum-recheck.ziw"
                    client.download(second["url"], checked)
                    if sha256(checked) != sha256(native): raise ApiError("Body changed during checksum anomaly recovery")
                    decrypt_current()
                    decrypted_for_recovery = True
                    rec["original_checksum"] = "server-md5-mismatch"
                    rec["source_checksum_anomaly"] = {"reported_md5": expected, "downloaded_md5": digest.hex(),
                        "downloaded_legacy_md5": legacy_md5(digest), "sha256": sha256(native),
                        "verification": "two-identical-downloads-and-local-decryption-zip-crc"}
                    rec["warnings"].append("Server body MD5 mismatch; stable ciphertext and decrypted ZIP CRC verified")
                else:
                    rec["original_checksum"] = "standard-md5" if expected == digest.hex() else "legacy-dword-md5"
                source_object_verified = True
            if info.get("dataSize") is not None and native.stat().st_size != int(info["dataSize"]):
                if not source_object_verified: raise ApiError("Original body size mismatch without a matching checksum")
                rec.setdefault("source_metadata_anomalies", []).append({"kind": "body_size", "reported": info["dataSize"],
                    "downloaded": native.stat().st_size, "verification": rec["original_checksum"]})
            archive = native
            if info.get("protected") and password_file and Path(password_file).exists():
                decrypted = obj / "decrypted.ziw"
                if not decrypted_for_recovery: decrypt_current()
                archive = decrypted
                rec["decrypted_locally"] = True
        elif "html" in payload:
            # REST exposes the original HTML/resources separately. Preserve those,
            # and create an explicitly reconstructed ZIW for portable offline use.
            body = payload["html"]
            atomic_bytes(obj / "index.html", body.encode("utf-8"))
            files = []
            resource_names = {}
            for resource_index, resource in enumerate(resources):
                name = safe_resource(resource["name"])
                if not name.startswith("index_files/"): name = "index_files/" + name
                target = obj / "resources" / name
                expected = resource.get("md5")
                if name in resource_names:
                    previous = resource_names[name]
                    if previous.get("url") == resource.get("url") and previous.get("size") == resource.get("size"):
                        rec["duplicate_resource_descriptors"] = rec.get("duplicate_resource_descriptors", 0) + 1
                        continue
                    candidate = obj / "duplicate-resources" / str(resource_index) / name
                    client.download(resource["url"], candidate, expected_md5=expected)
                    if sha256(candidate) != sha256(target):
                        raise ApiError("Conflicting duplicate resource names; both raw objects retained")
                    continue
                client.download(resource["url"], target, expected_md5=expected)
                if resource.get("size") is not None and target.stat().st_size != int(resource["size"]):
                    # Old notes can report the size of an obsolete shared CSS
                    # revision while the official endpoint serves its current
                    # revision. Require a stable second response and valid CSS;
                    # retain the original descriptor and the anomaly explicitly.
                    try: css = target.read_bytes().decode("utf-8-sig") if name.lower().endswith(".css") else ""
                    except UnicodeDecodeError: css = ""
                    if not css or "{" not in css or "}" not in css or css.lstrip().startswith(("<", "{")):
                        raise ApiError("Resource size mismatch")
                    checked = obj / "resource-rechecks" / name
                    client.download(resource["url"], checked, expected_md5=expected)
                    if sha256(checked) != sha256(target): raise ApiError("Resource changed during size-anomaly verification")
                    rec.setdefault("source_metadata_anomalies", []).append({"kind": "css_size", "name": name,
                        "reported": resource["size"], "downloaded": target.stat().st_size,
                        "verification": "two-identical-responses-with-transfer-checks"})
                files.append((target, name))
                resource_names[name] = resource
            part = archive.with_suffix(".ziw.part")
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(obj / "index.html", "index.html")
                z.writestr("backup-metadata.json", json.dumps(info, ensure_ascii=False))
                for target, name in files: z.write(target, name)
            part.replace(archive)
            rec["archive_kind"] = "reconstructed-from-original-html-and-resources"
        else:
            rec["warnings"].append("No standard HTML/ZIW body; collaborative document requires supplemental export")
            rec["status"] = "unsupported"
        dest = root / note_path(note)
        if note.get("type") == "collaboration" and not info.get("protected"):
            from .collaboration import archive_collaboration
            collaboration = archive_collaboration(client, note, obj / "collaboration")
            rec["collaboration"] = {k: v for k, v in collaboration.items() if k not in ("export",)}
            if collaboration.get("malformed_links"):
                rec.setdefault("source_metadata_anomalies", []).append({"kind": "malformed_navigation_links",
                    "count": collaboration["malformed_links"], "verification": "original-server-html-preserved"})
            if collaboration["status"] != "archived":
                rec["warnings"].append("Collaboration snapshot/resources incomplete")
            if collaboration.get("archive_path"):
                archive = Path(collaboration["archive_path"])
        if archive.exists():
            result = export_note(archive, dest, info)
            rec["status"] = result["status"]
            rec["export"] = result
            rec["readable_path"] = str(dest.relative_to(root))
            if result.get("missing_resources"):
                rec["warnings"].append("Missing local resources in exported HTML")
        attachments = client.api(f"/ks/note/attachments/{client.kb}/{guid}").get("result", [])
        write_json(obj / "attachments.json", attachments)
        rec["attachments"] = len(attachments)
        if len(attachments) != int(info.get("attachmentCount", len(attachments))):
            raise ApiError("Attachment count differs from document metadata")
        attachment_links = []
        html_attachments = []
        rec["attachment_validations"] = []
        for att in attachments:
            ag = att.get("attGuid", att.get("guid"))
            if not ag: raise ApiError("Attachment missing GUID")
            target = obj / "attachments" / (ag + ".bin")
            client.download(f"{client.server}/ks/object/download/{client.kb}/{guid}?objType=attachment&objId={ag}", target)
            # Modern endpoint usually returns an original file; old protocol may wrap it in ZIP.
            name = safe_name(att.get("name", ag), 160)
            local = dest / "attachments" / (ag + "--" + name)
            local.parent.mkdir(parents=True, exist_ok=True)
            from .attachments import materialize_attachment
            validation = materialize_attachment(target, att, local)
            rec["attachment_validations"].append({"attGuid": ag, **validation})
            relative = quote("attachments/" + local.name, safe="/")
            label = name.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            attachment_links.append(f"- [{label}]({relative})")
            html_attachments.append(f'<li><a href="{html.escape(relative, quote=True)}">{html.escape(name)}</a></li>')
        if attachment_links and (dest / "note.md").exists():
            with (dest / "note.md").open("a") as out: out.write("\n\n## Attachments\n\n" + "\n".join(attachment_links) + "\n")
        if html_attachments and (dest / "index.html").exists():
            page = (dest / "index.html").read_text()
            section = '<section class="backup-attachments"><h2>附件</h2><ul>' + "".join(html_attachments) + '</ul></section>'
            page = page.replace("</body>", section + "</body>") if "</body>" in page else page + section
            atomic_bytes(dest / "index.html", page.encode())
        if supplemental:
            # Preserve comments and history inventory, without any cloud mutation.
            for label, path, params in [
                ("comments", f"/ks/note/comments/{client.kb}/{guid}", {"extra": 1}),
                ("history", f"/ks/history/list/{client.kb}/{guid}", {"objType": "document", "objGuid": guid}),
            ]:
                try: write_json(obj / f"{label}.json", client.api(path, **params))
                except AuthError: raise
                except ApiError as exc: rec["warnings"].append(f"{label}: {exc}")
        for folder in [obj, dest]:
            if folder.exists():
                for path in sorted(folder.rglob("*")):
                    if path.is_file() and not path.name.endswith(".part"):
                        rec["files"].append({"path": str(path.relative_to(root)), "size": path.stat().st_size, "sha256": sha256(path)})
        rec["completed"] = time.time()
        write_json(record_path, rec)
        return rec, False
    except Exception as exc:
        rec["status"] = "auth_required" if isinstance(exc, AuthError) else "failed"
        # Only controlled exception messages are retained; no request URL/response text.
        rec["error"] = str(exc) if isinstance(exc, ApiError) else type(exc).__name__
        write_json(record_path, rec)
        return rec, False
    finally:
        client.session.close()


def run_backup(auth, root, *, network="direct", workers=4, limit=None, refresh=True, supplemental=True, password_file=None):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    client = WizClient(auth, network)
    notes = inventory(client, root) if refresh else json.loads((root / "metadata/notes.json").read_text())
    if limit is not None: notes = notes[:limit]
    counts = Counter()
    started = time.time()
    state = {"phase": "downloading", "total": len(notes), "completed": 0, "counts": {}, "updated": time.time()}
    iterator = iter(notes)
    stopped = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = set()
        def submit_next():
            n = next(iterator, None)
            if n is not None:
                pending.add(pool.submit(process_note, auth, network, root, n, supplemental=supplemental, password_file=password_file))
        for _ in range(workers): submit_next()
        while pending:
            done, pending = concurrent.futures.wait(pending, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                rec, reused = future.result()
                if rec["status"] == "auth_required": stopped = True
                counts[rec["status"]] += 1
                counts["reused"] += reused
                completed = sum(counts[k] for k in ("exported", "locked", "unsupported", "failed", "auth_required"))
                state = {"phase": "downloading", "total": len(notes), "completed": completed,
                         "counts": dict(counts), "elapsed_seconds": round(time.time() - started), "updated": time.time()}
                write_json(root / "progress.json", state)
                if completed == 1 or completed % 10 == 0 or completed == len(notes):
                    print(f"[{completed}/{len(notes)}] {100*completed/max(1,len(notes)):.1f}% {dict(counts)}", flush=True)
                if not stopped: submit_next()
    state["phase"] = "downloaded" if not counts["failed"] and not counts["auth_required"] else "needs_retry"
    write_json(root / "progress.json", state)
    return state


def verify(root):
    root = Path(root).resolve()
    inventory_notes = json.loads((root / "metadata/notes.json").read_text())
    current = {x["docGuid"]: x for x in inventory_notes}
    missing, corrupt, inconsistent, readability_issues, records = [], [], [], [], []
    count = 0
    for record_path in sorted((root / "records").glob("*.json")):
        try:
            rec = json.loads(record_path.read_text())
            if not isinstance(rec, dict) or not rec.get("guid"):
                raise ValueError("Invalid record schema")
        except (OSError, ValueError):
            inconsistent.append({"record": record_path.name, "reason": "unreadable_record"})
            continue
        records.append(rec)
        guid = rec["guid"]
        expected = current.get(guid, {})
        if record_path.stem != guid:
            inconsistent.append({"guid": guid, "reason": "record_filename_guid_mismatch"})
        exported = rec.get("export") or {}
        issues = [key for key in ("warnings", "missing_resources", "external_resources") if exported.get(key)]
        if rec.get("warnings"):
            issues.append("record_warnings")
        if issues:
            readability_issues.append({"guid": guid, "issues": issues})
        for entry in rec.get("files", []):
            relative = entry.get("path", "")
            count += 1
            path = (root / relative).resolve()
            if not relative or root not in path.parents:
                corrupt.append(relative or "[missing path]")
                continue
            if not path.is_file():
                missing.append(relative)
                continue
            try:
                if path.stat().st_size != entry.get("size") or sha256(path) != entry.get("sha256"):
                    corrupt.append(relative)
                if path.name == "metadata.json":
                    metadata = json.loads(path.read_text())
                    metadata = metadata.get("metadata", metadata)
                    if not isinstance(metadata, dict):
                        raise ValueError("Invalid metadata schema")
                    identity = metadata.get("docGuid", metadata.get("documentGuid", metadata.get("guid")))
                    if identity is not None and identity != guid:
                        inconsistent.append({"guid": guid, "reason": "metadata_guid_mismatch"})
                    if metadata.get("version") is not None and metadata["version"] != rec.get("version"):
                        inconsistent.append({"guid": guid, "reason": "metadata_version_mismatch"})
                    if expected.get("kbGuid") and metadata.get("kbGuid") is not None and metadata["kbGuid"] != expected["kbGuid"]:
                        inconsistent.append({"guid": guid, "reason": "metadata_kb_mismatch"})
            except (OSError, ValueError, AttributeError):
                inconsistent.append({"guid": guid, "reason": "unreadable_file_or_metadata", "path": relative})
    seen = {x["guid"] for x in records}
    result = {"files_checked": count, "missing_files": missing, "corrupt_files": corrupt,
              "inventory_notes": len(inventory_notes), "records": len(records),
              "notes_without_record": [n["docGuid"] for n in inventory_notes if n["docGuid"] not in seen],
              "statuses": dict(Counter(r.get("status", "invalid") for r in records)),
              "warnings": len(readability_issues), "readability_issues": readability_issues,
              "inconsistent_records": inconsistent, "time": time.time()}
    result["source_checksum_anomalies"] = sum(bool(r.get("source_checksum_anomaly")) for r in records)
    result["stale_records"] = [r["guid"] for r in records if r["guid"] in current and r.get("version") != current[r["guid"]]["version"]]
    result["failed_records"] = [r["guid"] for r in records if r.get("status") not in ("exported", "locked") or not r.get("files")]
    result["integrity_ok"] = not any((missing, corrupt, inconsistent, result["notes_without_record"], result["stale_records"], result["failed_records"]))
    result["all_readable"] = result["integrity_ok"] and not readability_issues and all(r.get("status") == "exported" for r in records)
    write_json(root / "verification.json", result)
    return result


def pack(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output == root or root in output.parents: raise ApiError("Archive must be outside the backup directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    required = sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink())
    if shutil.disk_usage(output.parent).free < required + 1024**3:
        raise ApiError("Insufficient free space for a safe archive; existing files preserved")
    # A timestamped ZIP64 archive; callers select a new destination instead of overwriting.
    if output.exists(): raise ApiError("Archive destination already exists")
    part = output.with_name(output.name + ".part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=4, allowZip64=True) as z:
        for p in sorted(root.rglob("*")):
            if p.is_file() and not p.name.endswith(".part") and not p.is_symlink():
                z.write(p, str(Path(root.name) / p.relative_to(root)))
    with zipfile.ZipFile(part) as z:
        bad = z.testzip()
        if bad: raise ApiError("Archive CRC failed")
    part.replace(output)
    atomic_bytes(output.with_suffix(output.suffix + ".sha256"), f"{sha256(output)}  {output.name}\n".encode())
    return {"archive": str(output), "bytes": output.stat().st_size, "sha256": sha256(output), "crc_ok": True}
