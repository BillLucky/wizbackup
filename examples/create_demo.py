#!/usr/bin/env python3
"""Create an entirely synthetic, network-free archive for recovery drills.

Run from a source checkout after installing the package:
    python examples/create_demo.py --output .demo

No account, token, password, captured response, or personal note is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import zipfile

from wizbackup.attachments import materialize_attachment
from wizbackup.backup import note_path, pack, sha256, verify
from wizbackup.catalog import finish_catalog
from wizbackup.client import write_json
from wizbackup.exporter import export_note

PERSONAL_KB = "11111111-1111-4111-8111-111111111111"
TEAM_KB = "22222222-2222-4222-8222-222222222222"
PERSONAL_NOTE = "33333333-3333-4333-8333-333333333333"
TEAM_NOTE = "44444444-4444-4444-8444-444444444444"
ATTACHMENT = "55555555-5555-4555-8555-555555555555"


def create_library(root: Path, *, team=False):
    """Create one checked synthetic library; intended for examples and tests."""
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("Synthetic demo library destination must be empty")
    kb, guid = (TEAM_KB, TEAM_NOTE) if team else (PERSONAL_KB, PERSONAL_NOTE)
    note = {"kbGuid": kb, "docGuid": guid, "version": 1,
            "title": "Synthetic team note" if team else "Synthetic personal note",
            "category": "/Demo/", "type": "document", "protected": 0,
            "attachmentCount": 1, "created": 1577836800000, "dataModified": 1577836800000}
    write_json(root / "metadata/notes.json", [note])
    write_json(root / "metadata/categories.json", {"result": ["/Demo/", "/Empty/"]})
    write_json(root / "metadata/tags.json", {"result": [{"tagGuid": "synthetic-tag", "name": "demo"}]})
    obj = root / "objects" / guid / "1"
    obj.mkdir(parents=True)
    write_json(obj / "metadata.json", note)
    archive = obj / "content.ziw"
    body = ('<!doctype html><html><head><meta charset="utf-8"></head><body>'
            '<h1 id="demo">Synthetic archive / 合成示例</h1>'
            '<p>This demonstration contains no personal data.</p>'
            '<img src="index_files/demo.svg" alt="Synthetic blue circle">'
            '<p><a href="wiz://open_document?guid=' + guid + '#demo">Local self-link</a></p>'
            '</body></html>')
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><circle cx="40" cy="40" r="30" fill="#286bc1"/></svg>'
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("index.html", body)
        zipped.writestr("index_files/demo.svg", svg)
        zipped.writestr("backup-metadata.json", json.dumps(note))
    attachment = b"Synthetic attachment. This is not a real user document.\n"
    att = {"attGuid": ATTACHMENT, "docGuid": guid, "kbGuid": kb,
           "name": "demo attachment.txt", "dataSize": len(attachment),
           "dataMd5": hashlib.md5(attachment).hexdigest()}
    write_json(obj / "attachments.json", [att])
    raw = obj / "attachments" / (ATTACHMENT + ".bin")
    raw.parent.mkdir(); raw.write_bytes(attachment); raw.chmod(0o600)
    dest = root / note_path(note)
    exported = export_note(archive, dest, note)
    local = dest / "attachments" / (ATTACHMENT + "--demo attachment.txt")
    validation = materialize_attachment(raw, att, local)
    # Use the same safe formatting used by the offline recovery implementation.
    from wizbackup.offline import _append_attachments
    _append_attachments(dest, [("demo attachment.txt", "attachments/" + ATTACHMENT + "--demo%20attachment.txt")])
    record = {"guid": guid, "version": 1, "status": "exported", "warnings": [],
              "readable_path": dest.relative_to(root).as_posix(), "export": exported,
              "attachments": 1, "attachment_validations": [validation],
              "files": [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size, "sha256": sha256(p)}
                        for folder in (obj, dest) for p in sorted(folder.rglob("*")) if p.is_file()]}
    write_json(root / "records" / (guid + ".json"), record)
    (root / "readable/Empty").mkdir(parents=True)
    if not finish_catalog(root)["ok"] or not verify(root)["all_readable"]:
        raise RuntimeError("Synthetic demo verification failed")
    return root


def create_demo(output: Path, *, library_only=False):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Demo output must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    if library_only:
        library = create_library(output / "personal")
        archive = pack(library, output / "personal.zip")
        return {"synthetic": True, "library": "personal", "archive": Path(archive["archive"]).name}
    account = output / "account"
    create_library(account / "personal")
    create_library(account / "teams" / TEAM_KB, team=True)
    write_json(account / "knowledge-bases.json", [
        {"kb_guid": PERSONAL_KB, "kind": "personal", "path": "personal"},
        {"kb_guid": TEAM_KB, "kind": "team", "path": "teams/" + TEAM_KB}])
    archive = pack(account, output / "synthetic-account.zip")
    return {"synthetic": True, "account": "account", "personal_library": "account/personal",
            "team_library": "account/teams/" + TEAM_KB, "archive": Path(archive["archive"]).name}


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--library-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(create_demo(args.output, library_only=args.library_only), indent=2))
