#!/usr/bin/env python3
"""Exercise installed CLI with synthetic data and networking disabled."""
from pathlib import Path
import argparse
import json
import os
import runpy
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BLOCKED_RUNNER = """
import socket, requests, sys
from unittest.mock import patch
from wizbackup.cli import main
def blocked(*args, **kwargs): raise AssertionError('Network disabled during offline smoke test')
with patch.object(socket.socket, 'connect', blocked), patch.object(socket, 'getaddrinfo', blocked), patch.object(requests.sessions.Session, 'request', blocked):
    raise SystemExit(main(sys.argv[1:]))
"""

def run(output):
    output = Path(output).resolve()
    if output.exists() and any(output.iterdir()): raise ValueError("Smoke output must be new or empty")
    output.mkdir(parents=True, exist_ok=True)
    create = runpy.run_path(str(ROOT / "examples/create_demo.py"))["create_demo"]
    create(output / "demo")
    library = output / "demo/account/personal"
    steps = [
        ("inspect", ["inspect", "--source", str(library)]),
        ("restore", ["restore", "--source", str(output / "demo/synthetic-account.zip"), "--destination", str(output / "restored")]),
        ("rebuild", ["rebuild", "--source", str(library), "--destination", str(output / "rebuilt")]),
        ("vault", ["vault", "--source", str(output / "rebuilt"), "--destination", str(output / "vault")]),
        ("verify", ["verify", "--output", str(output / "rebuilt")]),
    ]
    results = {}
    for name, command in steps:
        process = subprocess.run([sys.executable, "-c", BLOCKED_RUNNER, *command], cwd=output, capture_output=True, text=True)
        if process.returncode != 0:
            raise RuntimeError("Offline CLI smoke step failed: " + name + "\n" + process.stderr)
        result = json.loads(process.stdout)
        if result.get("network_used") is True or result.get("ok") is False:
            raise RuntimeError("Offline CLI smoke invariant failed: " + name)
        results[name] = {"exit_code": process.returncode, "status": result.get("status", "verified")}
    report = {"ok": True, "synthetic_only": True, "network_disabled": True, "steps": results}
    (output / "smoke-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report

if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    target = args.output or Path(tempfile.mkdtemp(prefix="wizbackup-offline-smoke-")) / "run"
    print(json.dumps(run(target), indent=2))
