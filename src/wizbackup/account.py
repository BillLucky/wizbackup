"""Discover current accessible libraries and keep each library isolated."""
from __future__ import annotations
import json
import time
import uuid
from pathlib import Path
from .client import WizClient, write_json, ApiError
from .backup import run_backup, verify, pack


def discover(auth, network="direct"):
    c = WizClient(auth, network)
    try:
        payload = c.api("https://as.wiz.cn/as/groups")
        result = payload.get("result", {})
        groups = result.get("groupList", []) if isinstance(result, dict) else result
        bases = [{"kb_guid": auth["kb_guid"], "kb_server": auth["kb_server"], "kind": "personal", "path": "personal"}]
        seen = {auth["kb_guid"]}
        for g in groups:
            guid = g.get("kbGuid")
            if not guid or guid in seen: continue
            try: uuid.UUID(guid)
            except (ValueError, TypeError, AttributeError): raise ApiError("Library has an invalid GUID") from None
            if not g.get("kbServer"): raise ApiError("Library has no server address")
            bases.append({"kb_guid": guid, "kb_server": g["kbServer"], "kind": g.get("type", "team"),
                          "path": "teams/" + guid, "biz_guid": g.get("bizGuid"), "encrypted": g.get("isEncrypt", False)})
            seen.add(guid)
        return bases
    finally: c.session.close()


def run_account(auth, root, *, network="direct", workers=6, password_file=None, refresh=True, archives=True, postprocess=True, external_proxy=None):
    root = Path(root).resolve(); root.mkdir(parents=True, exist_ok=True);root.chmod(0o700)
    bases = discover(auth, network)
    write_json(root / "knowledge-bases.json", bases)
    reports = []
    for base in bases:
        local_auth = {**auth, "kb_guid": base["kb_guid"], "kb_server": base["kb_server"]}
        local = root / base["path"]
        state = run_backup(local_auth, local, network=network, workers=workers, password_file=password_file,
                           refresh=refresh or not (local / "metadata/notes.json").exists())
        if state["phase"] == "needs_retry" and not state.get("counts", {}).get("auth_required"):
            state = run_backup(local_auth, local, network=network, workers=workers, password_file=password_file, refresh=False)
        report = {"kind": base["kind"], "path": base["path"], "backup": state, "verification": verify(local)}
        if state.get("counts", {}).get("auth_required"):
            report["stopped_reason"] = "authentication_required"
            reports.append(report)
            write_json(root / "account-summary.json", reports)
            break
        reports.append(report)
        write_json(root / "account-summary.json", reports)
    # Secure all current libraries before spending time on external web pages
    # or compression. Each subsequent stage changes only local reading copies.
    if any(report.get("stopped_reason") for report in reports): return reports
    for report in reports:
        if report.get("stopped_reason") or not report["verification"]["integrity_ok"]:
            continue
        local = root / report["path"]
        def phase(name):
            write_json(local / "progress.json", {**report["backup"], "phase": name, "updated": time.time()})
        if postprocess:
            from .external import mirror_external
            from .catalog import finish_catalog
            phase("mirroring-external")
            report["external_resources"] = mirror_external(local, proxy=external_proxy)
            phase("catalog")
            report["catalog"] = finish_catalog(local)
            phase("verifying")
            report["verification"] = verify(local)
            if not report["catalog"].get("ok"):
                report["postprocess_error"] = "Catalog integrity precheck failed"
        if archives and report["verification"]["integrity_ok"] and not report.get("postprocess_error"):
            phase("packaging")
            dest = root.parent / "archives" / (report["path"].replace("/", "-") + "-" + time.strftime("%Y%m%d-%H%M%S") + ".zip")
            report["archive"] = pack(local, dest)
            phase("archived" if report["verification"]["all_readable"] else "archived-with-gaps")
        else:
            phase("verified" if report["verification"]["integrity_ok"] else "needs_retry")
        write_json(root / "account-summary.json", reports)
    return reports
