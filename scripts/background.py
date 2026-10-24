#!/usr/bin/env python3
"""Start a process-local job; keep runtime files outside the software checkout."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from wizbackup.client import normalized_proxy

os.umask(0o077)
root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("job", choices=["backup", "dashboard"])
parser.add_argument("--data-dir", type=Path, default=Path.home() / "WizBackupData")
parser.add_argument("--no-refresh", action="store_true")
parser.add_argument("--workers", type=int, default=4)
parser.add_argument("--port", type=int, default=8765)
parser.add_argument("--proxy")
parser.add_argument("--external-proxy", default="direct")
args = parser.parse_args()
data = args.data_dir.expanduser().resolve()
if data == root or root in data.parents:
    parser.error("Choose a data directory outside the software checkout")
for value in (args.proxy, args.external_proxy):
    normalized_proxy(value)
private = data / "private"
if private.is_symlink():
    parser.error("Private runtime directory must not be a symlink")
private.mkdir(parents=True, exist_ok=True, mode=0o700)
private.chmod(0o700)
destination = data / "backups/account"
if args.job == "backup":
    destination.mkdir(parents=True, exist_ok=True)
    with (destination / ".backup.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Backup already running; inspect its progress")
    command = [sys.executable, "-u", "-m", "wizbackup.cli", "backup-all",
               "--auth", str(private / "auth.json"),
               "--password-file", str(private / "decryption-password.txt"),
               "--output", str(destination), "--workers", str(max(1, min(args.workers, 8))),
               "--external-proxy", args.external_proxy]
    if args.no_refresh:
        command.append("--no-refresh")
    if args.proxy:
        command += ["--proxy", args.proxy]
else:
    command = [sys.executable, "-u", "-m", "wizbackup.cli", "serve",
               "--output", str(destination), "--port", str(args.port)]
with (private / (args.job + "-run.log")).open("ab") as log:
    process = subprocess.Popen(command, cwd=data, stdin=subprocess.DEVNULL, stdout=log,
                               stderr=log, start_new_session=True, close_fds=True)
record = {"pid": process.pid, "started": time.time(), "job": args.job,
          "data_directory": str(data), "log": str(log.name)}
if args.job == "backup" and Path("/usr/bin/caffeinate").exists():
    hold = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(process.pid)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True)
    record["temporary_idle_sleep_hold_pid"] = hold.pid
try:
    record["git_head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root,
                                                 stderr=subprocess.DEVNULL, text=True).strip()
except subprocess.CalledProcessError:
    record["git_head"] = None
(private / (args.job + "-process.json")).write_text(json.dumps(record, indent=2) + "\n")
print(json.dumps({"pid": process.pid, "job": args.job, "log": record["log"]}))
