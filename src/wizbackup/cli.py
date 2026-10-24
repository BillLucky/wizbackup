from __future__ import annotations
import argparse
import fcntl
import json
import os
import sys
from pathlib import Path

from .backup import run_backup, verify, pack
from .client import ApiError
from . import __version__


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="WizNote private, resumable, read-only backup")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("command", choices=["setup", "backup", "backup-all", "discover", "status", "verify", "pack", "serve", "catalog", "history", "mirror-external", "inspect", "restore", "rebuild", "vault"])
    parser.add_argument("--auth", default=".private/auth.json", type=Path)
    parser.add_argument("--output", type=Path, help="Backup/library destination; defaults depend on command")
    parser.add_argument("--source", type=Path, help="Offline input archive or library directory")
    parser.add_argument("--destination", type=Path, help="New, separate offline output directory")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / "WizBackupData", help="Private runtime directory outside the software checkout")
    parser.add_argument("--from-auth", type=Path, help="Import an already-authorized private local JSON file without chat secrets")
    parser.add_argument("--network", choices=["direct", "us"], default="direct")
    parser.add_argument("--proxy", help="Explicit HTTP(S) proxy for Wiz requests; no ambient proxy inheritance")
    parser.add_argument("--external-proxy", default="direct", help="Independent HTTP(S) proxy or direct for third-party resources")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--no-supplemental", action="store_true")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--password-file", type=Path, help="Explicit local note-encryption password file (offline commands never infer credentials)")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-archives", action="store_true")
    parser.add_argument("--no-postprocess", action="store_true", help="Skip external mirroring and local catalog generation")
    parser.add_argument("--guid", help="Single document GUID for an explicit history retry")
    args = parser.parse_args(argv)
    if args.output is None:
        args.output = Path("backups/account" if args.command in ("backup-all", "discover", "serve", "status") else "backups/wiznote")
    try:
        if args.command == "setup":
            from .setup import configure
            selected = args.proxy if args.proxy is not None else ("http://127.0.0.1:7897" if args.network == "us" else None)
            result = configure(args.data_dir, from_auth=args.from_auth, proxy=selected)
        elif args.command in ("inspect", "restore", "rebuild", "vault"):
            from .offline import inspect_archive, restore_archive, rebuild_readable, export_vault
            if args.source is None: raise ApiError("--source is required")
            if args.command != "inspect" and args.destination is None: raise ApiError("--destination is required")
            if args.command == "inspect": result = inspect_archive(args.source)
            elif args.command == "restore": result = restore_archive(args.source, args.destination)
            elif args.command == "rebuild": result = rebuild_readable(args.source, args.destination, password_file=args.password_file)
            else: result = export_vault(args.source, args.destination)
        elif args.command == "serve":
            from .dashboard import serve
            serve(args.output, port=args.port)
            return 0
        elif args.command in ("backup", "backup-all", "discover"):
            args.output.mkdir(parents=True, exist_ok=True)
            with (args.output / ".backup.lock").open("a") as lock:
                try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError: raise ApiError("Another backup is already running for this destination")
                if args.auth.stat().st_mode & 0o077: raise ApiError("Auth file must have mode 600")
                auth = json.loads(args.auth.read_text())
                if args.password_file is None and args.command in ("backup", "backup-all"):
                    candidate = args.auth.parent / "decryption-password.txt"
                    if candidate.is_file(): args.password_file = candidate
                if args.proxy is not None: auth["proxy"] = args.proxy
                if args.command == "backup-all":
                    from .account import run_account
                    result = run_account(auth, args.output, network=args.network, workers=max(1, min(args.workers, 8)),
                                         refresh=not args.no_refresh, password_file=args.password_file,
                                         archives=not args.no_archives, postprocess=not args.no_postprocess,
                                         external_proxy=args.external_proxy)
                elif args.command == "discover":
                    from .account import discover
                    result = discover(auth, args.network)
                else:
                    result = run_backup(auth, args.output, network=args.network, workers=max(1, min(args.workers, 8)),
                                        limit=args.limit, refresh=not args.no_refresh, supplemental=not args.no_supplemental,
                                        password_file=args.password_file)
        elif args.command == "catalog":
            from .catalog import finish_catalog
            result = finish_catalog(args.output)
        elif args.command == "mirror-external":
            from .external import mirror_external
            result = mirror_external(args.output, proxy=args.external_proxy)
        elif args.command == "history":
            from .history import archive_history
            from .client import WizClient, write_json
            if not args.guid: raise ApiError("--guid is required for a controlled history retry")
            notes = json.loads((args.output / "metadata/notes.json").read_text())
            note = next((n for n in notes if n["docGuid"] == args.guid), None)
            if not note: raise ApiError("Document GUID is not present in the saved inventory")
            auth = json.loads(args.auth.read_text())
            if args.proxy is not None: auth["proxy"] = args.proxy
            auth["kb_guid"] = note["kbGuid"]
            bases_path = args.output.parent.parent / "knowledge-bases.json"
            if bases_path.exists():
                base = next((b for b in json.loads(bases_path.read_text()) if b["kb_guid"] == note["kbGuid"]), None)
                if base: auth["kb_server"] = base["kb_server"]
            c = WizClient(auth, args.network)
            try:
                payload = c.api(f"/ks/history/list/{c.kb}/{args.guid}", objType="document", objGuid=args.guid)
                result = archive_history(c, note, payload, args.output / "history-retries" / args.guid)
            finally: c.session.close()
        elif args.command == "status":
            if (args.output / "knowledge-bases.json").exists():
                from .dashboard import status_snapshot
                result = status_snapshot(args.output)
            else: result = json.loads((args.output / "progress.json").read_text())
        elif args.command == "verify": result = verify(args.output)
        else:
            if not args.archive: raise ApiError("--archive is required")
            result = pack(args.output, args.archive)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "backup" and result["phase"] == "needs_retry": return 2
        if args.command == "backup-all":
            bases_file = args.output / "knowledge-bases.json"
            expected = len(json.loads(bases_file.read_text())) if bases_file.exists() else 0
            if len(result) != expected or any(not r.get("verification", {}).get("integrity_ok") for r in result): return 2
            if any(not r.get("verification", {}).get("all_readable") for r in result): return 3
        if args.command == "verify":
            if not result["integrity_ok"]: return 2
            if not result["all_readable"]: return 3
        if args.command == "mirror-external":
            if result.get("skipped_invalid_records"): return 2
            if result.get("resources_failed") or result.get("manifest_schema_errors"): return 3
        if args.command == "catalog" and not result.get("ok"): return 4
        if args.command == "history" and result.get("status") != "archived": return 5
        if args.command in ("inspect", "restore", "rebuild", "vault"):
            if result.get("ok") is False or result.get("integrity_ok") is False: return 2
            if result.get("all_readable") is False or result.get("status") in ("partial", "locked"): return 3
        return 0
    except (ApiError, OSError, ValueError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__": sys.exit(main())
