#!/usr/bin/env python3
"""Audit only distributable files; private values are supplied outside checkout."""
from __future__ import annotations
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import zipfile

FORBIDDEN_PARTS = {".private", "backups", "archives", ".demo", ".venv", "__pycache__", "client-snapshot"}
FORBIDDEN_SUFFIXES = {".ziw", ".ziwr", ".db", ".sqlite", ".sqlite3", ".har", ".pem", ".key", ".pyc", ".zip", ".whl", ".gz", ".rar", ".7z"}
MAX_MEMBER_BYTES = 5 * 1024 * 1024
PATTERNS = [
    ("github-credential", re.compile(rb"(?:gh[pousr]_[A-Za-z0-9_]{24,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("cloud-access-key", re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    ("private-key-body", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{40,}")),
    ("personal-home-path", re.compile(rb"/Users/[A-Za-z][A-Za-z0-9_.-]+/")),
]

def path_problem(name):
    p = PurePosixPath(name.replace("\\", "/"))
    if p.is_absolute() or ".." in p.parts:
        return "unsafe-distribution-path"
    if set(p.parts) & FORBIDDEN_PARTS or p.suffix.lower() in FORBIDDEN_SUFFIXES:
        return "private-runtime-file"
    if p.name == ".env" or p.name.startswith(".env.") and p.name != ".env.example":
        return "credential-file"
    return None

def scan_bytes(name, data, private_values=()):
    reasons = set()
    if len(data) > MAX_MEMBER_BYTES: reasons.add("unexpected-large-file")
    if any(value and value in data for value in private_values): reasons.add("private-value-match")
    for label, pattern in PATTERNS:
        if pattern.search(data): reasons.add(label)
    # Session literals must be recognizable placeholders, not copied live tokens.
    for match in re.finditer(rb'''(?i)["'](?:x-wiz-token|token)["']\s*:\s*["']([A-Za-z0-9_-]{32,})["']''', data):
        value = match[1].lower()
        if not any(word in value for word in (b"synthetic", b"example", b"your_", b"placeholder", b"test", b"demo")):
            reasons.add("session-token-literal")
    return [{"file": name, "reason": reason} for reason in sorted(reasons)]

def audit(root, *, private_values=(), history=False, artifacts=None):
    root = Path(root).resolve()
    names = [s for s in subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0") if s]
    if not names: raise ValueError("Stage the intended publication files before auditing")
    findings = []
    for name in names:
        path = root / name
        problem = path_problem(name)
        if problem: findings.append({"file": name, "reason": problem})
        if path.is_symlink():
            findings.append({"file": name, "reason": "tracked-symlink"})
            continue
        if path.is_file(): findings.extend(scan_bytes(name, path.read_bytes(), private_values))
    blobs = 0
    if history:
        rows = subprocess.check_output(["git", "cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)"], cwd=root, text=True).splitlines()
        for row in rows:
            oid, kind = row.split()
            if kind == "blob":
                blobs += 1
                data = subprocess.check_output(["git", "cat-file", "blob", oid], cwd=root)
                findings.extend(scan_bytes("git-blob:" + oid, data, private_values))
    distributions = 0
    if artifacts:
        for file in sorted(Path(artifacts).glob("*")):
            if file.suffix in (".whl", ".zip"):
                distributions += 1
                with zipfile.ZipFile(file) as archive:
                    for info in archive.infolist():
                        problem = path_problem(info.filename)
                        if problem: findings.append({"file": file.name + ":" + info.filename, "reason": problem})
                        if info.file_size > MAX_MEMBER_BYTES:
                            findings.append({"file": file.name, "reason": "oversized-distribution-member"})
                        elif not info.is_dir():
                            findings.extend(scan_bytes(file.name + ":" + info.filename, archive.read(info), private_values))
            elif file.name.endswith(".tar.gz"):
                distributions += 1
                with tarfile.open(file, "r:gz") as archive:
                    for info in archive:
                        problem = path_problem(info.name)
                        if problem: findings.append({"file": file.name + ":" + info.name, "reason": problem})
                        if info.issym() or info.islnk():
                            findings.append({"file": file.name, "reason": "distribution-link"})
                        if info.size > MAX_MEMBER_BYTES:
                            findings.append({"file": file.name, "reason": "oversized-distribution-member"})
                        elif info.isfile():
                            with archive.extractfile(info) as stream:
                                findings.extend(scan_bytes(file.name + ":" + info.name, stream.read(), private_values))
    return {"ok": not findings, "tracked_files": len(names), "historical_blobs": blobs,
            "distributions": distributions, "findings": findings}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--private-values-file", type=Path, help="Optional private JSON list outside checkout; values are never printed")
    args = parser.parse_args()
    values = []
    if args.private_values_file:
        source = args.private_values_file.resolve()
        if source == args.root.resolve() or args.root.resolve() in source.parents:
            raise SystemExit("Private audit inputs must remain outside the publication checkout")
        if source.stat().st_mode & 0o077: raise SystemExit("Private audit input must have mode 600")
        raw = json.loads(source.read_text())
        if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
            raise SystemExit("Private audit input must be a JSON list of strings")
        values = [v.encode() for v in raw if v]
    result = audit(args.root, private_values=values, history=args.history, artifacts=args.artifacts)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1

if __name__ == "__main__": raise SystemExit(main())
