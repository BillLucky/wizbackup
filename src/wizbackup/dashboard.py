"""Read-only, loopback-only backup progress dashboard.

This service deliberately exposes counters, timestamps and fixed status labels
only. It does not serve the backup directory or return document metadata.
"""
from __future__ import annotations

import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import re
import stat

_PHASES = {"mirroring-external", "archived-with-gaps",
    "waiting", "inventory", "downloading", "downloaded", "needs_retry",
    "verifying", "verified", "packaging", "archived", "complete",
    "auth_required", "paused", "unknown", "inventory-crosscheck", "catalog",
    "cataloging", "retrying", "discovering", "discovering-bases",
    "account-waiting", "not-started",
}
_COUNTS = ("exported", "locked", "unsupported", "failed", "auth_required", "reused")
_EXTERNAL_PHASES = {"waiting", "mirroring", "complete", "partial", "unknown"}
_SCRIPT = """'use strict';
const phases = {waiting:'等待备份启动',inventory:'扫描云端目录','inventory-crosscheck':'交叉核对目录',downloading:'下载中',downloaded:'下载处理结束',needs_retry:'有项目需要重试',verifying:'正在校验',verified:'校验结束',packaging:'正在压缩',archived:'压缩完成',complete:'备份完成',auth_required:'需要更新登录凭据',paused:'已暂停',unknown:'状态待确认',catalog:'生成离线目录',cataloging:'生成离线目录',retrying:'重试未完成项目',discovering:'发现知识库','discovering-bases':'发现知识库','account-waiting':'等待其余知识库开始','not-started':'已发现，尚未开始','mirroring-external':'补全外链图片与样式','archived-with-gaps':'已归档，部分外部资源不可用'};
const externalPhases = {waiting:'等待其余知识库外链阶段',mirroring:'正在补全外链图片与样式',complete:'外链资源处理完成',partial:'外链处理结束，部分资源不可用',unknown:'外链状态待确认'};
const el = id => document.getElementById(id);
function number(value) { return Number.isFinite(value) ? value.toLocaleString('zh-CN') : '—'; }
function stamp(value) { return value ? new Date(value * 1000).toLocaleString('zh-CN') : '—'; }
function knownTotal(p) { return p.total_known !== false && Number.isFinite(p.total); }
function progressText(p) { return number(p.completed) + ' / ' + (knownTotal(p) ? number(p.total) : '总数待确认'); }
function externalText(p) { return number(p.completed) + ' / ' + number(p.discovered); }
function renderBases(data) {
  const section = el('bases');
  const container = el('base-cards');
  section.hidden = data.mode !== 'account';
  container.replaceChildren();
  if (data.mode !== 'account') { el('scope').textContent = ''; return; }
  el('scope').textContent = '已发现 ' + number(data.bases.length) + ' 个知识库；' + number(data.progress.unknown_total_bases) + ' 个库总数待确认' + (data.invalid_bases ? '；有配置未通过本地路径校验' : '');
  for (const base of data.bases) {
    const card = document.createElement('article'); card.className = 'base-card';
    const heading = document.createElement('h3'); heading.textContent = base.label;
    const stage = document.createElement('p'); stage.className = 'muted'; stage.textContent = phases[base.progress.phase] || phases.unknown;
    const done = document.createElement('p'); done.textContent = '已完成处理：' + progressText(base.progress);
    const counts = base.progress.counts;
    const stats = document.createElement('p'); stats.textContent = '成功 ' + number(counts.exported) + ' · 加密待解 ' + number(counts.locked) + ' · 失败 ' + number(counts.failed + counts.auth_required);
    const updated = document.createElement('p'); updated.className = 'muted'; updated.textContent = '最近更新：' + stamp(base.progress.updated);
    card.append(heading, stage, done, stats, updated); container.append(card);
    if (base.external_progress) {
      const external = document.createElement('p'); external.textContent = '外链资源：' + externalText(base.external_progress) + ' · ' + (externalPhases[base.external_progress.phase] || externalPhases.unknown); card.append(external);
    }
    if (!knownTotal(base.progress) && Number.isFinite(base.progress.discovered)) {
      const discovered = document.createElement('p'); discovered.textContent = '扫描中已发现笔记：' + number(base.progress.discovered); card.append(discovered);
    }
  }
}
async function refresh() {
  try {
    const response = await fetch('/status', {cache:'no-store'});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    const p = data.progress;
    const counts = p.counts;
    const known = knownTotal(p);
    const percent = known && p.total > 0 ? Math.min(100, Math.max(0, p.completed / p.total * 100)) : 0;
    el('phase').textContent = phases[p.phase] || phases.unknown;
    if (known) el('bar').value = percent; else el('bar').removeAttribute('value');
    el('percentage').textContent = known ? percent.toFixed(1) + '%' : '总数待确认';
    el('processed').textContent = progressText(p);
    el('success').textContent = number(counts.exported);
    el('locked').textContent = number(counts.locked);
    el('failed').textContent = number(counts.failed + counts.auth_required);
    el('unsupported').textContent = number(counts.unsupported);
    el('reused').textContent = number(counts.reused);
    el('updated').textContent = stamp(p.updated);
    const external = data.external_progress;
    el('external').hidden = !external;
    if (external) {
      el('external-phase').textContent = externalPhases[external.phase] || externalPhases.unknown;
      el('external-count').textContent = externalText(external);
      el('external-bar').value = external.discovered > 0 ? Math.min(100, Math.max(0, external.completed / external.discovered * 100)) : external.phase === 'complete' ? 100 : 0;
      el('external-bytes').textContent = number(external.network_bytes / 1048576) + ' MiB';
      el('external-updated').textContent = stamp(external.updated);
    }
    renderBases(data);
    const v = data.verification;
    el('verification').textContent = !v ? '尚无校验结果' : v.integrity_ok === true ? '已登记文件校验通过' : v.integrity_ok === false ? '校验发现待处理项目' : '校验状态待确认';
    el('verified').textContent = v ? number(v.files_checked) + ' 个文件；' + (data.mode === 'account' ? number(v.verified_bases) + ' / ' + number(v.total_bases) + ' 个库已有校验结果；' : '') + '更新时间 ' + stamp(v.time) : '—';
    el('connection').textContent = data.available ? '每 2 秒自动更新' : '等待进度文件';
  } catch (_) {
    el('connection').textContent = '暂时无法读取进度，稍后自动重试';
  } finally {
    window.setTimeout(refresh, 2000);
  }
}
refresh();
"""
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
_CSP = ("default-src 'none'; script-src 'sha256-" + _SCRIPT_HASH + "'; "
        "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'; object-src 'none'")
_PAGE = ("""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>为知笔记备份进度</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#16202c}main{max-width:920px;margin:56px auto;padding:0 24px}h1{font-size:28px;margin:0 0 8px}.muted{color:#596775}.panel{padding:26px;background:white;border:1px solid #dbe2e8;border-radius:14px;margin-top:24px}.progress-label{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:16px}h2{font-size:20px;margin:0}progress{display:block;appearance:none;width:100%;height:14px;border:0;border-radius:10px;overflow:hidden;background:#e4ebf0}progress::-webkit-progress-bar{background:#e4ebf0}progress::-webkit-progress-value{background:#237ba7}progress::-moz-progress-bar{background:#237ba7}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;margin-top:26px}.stat dt{font-size:14px;color:#596775;margin-bottom:7px}.stat dd{font-size:27px;font-weight:600;margin:0}.stat{margin:0}.foot{margin-top:24px;font-size:14px;line-height:1.8}.check{font-size:15px;line-height:1.8}.check p{margin:0}#connection{font-size:14px}#scope{font-size:14px;line-height:1.8;margin:0 0 16px}#base-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.base-card{border:1px solid #dbe2e8;border-radius:10px;padding:18px;min-width:0}.base-card h3{font-size:17px;margin:0 0 12px}.base-card p{font-size:14px;line-height:1.6;margin:7px 0}a{color:inherit}[hidden]{display:none!important}@media(max-width:600px){main{margin:28px auto;padding:0 16px}.panel{padding:20px}.grid{grid-template-columns:repeat(2,1fr)}#base-cards{grid-template-columns:1fr}h1{font-size:24px}.stat dd{font-size:23px}}@media(prefers-color-scheme:dark){body{background:#141b22;color:#e7edf4}.panel{background:#1b2631;border-color:#344352}.base-card{border-color:#344352}.muted,.stat dt{color:#b4c1ce}progress,progress::-webkit-progress-bar{background:#344352}}
</style></head><body><main>
<h1>为知笔记备份进度</h1><div class="muted">本机只读看板 · 不展示笔记内容</div>
<section class="panel" aria-labelledby="phase"><div class="progress-label"><h2 id="phase">等待备份启动</h2><strong id="percentage">0.0%</strong></div>
<progress id="bar" max="100" value="0" aria-label="备份处理进度"></progress>
<div class="grid"><dl class="stat"><dt>已完成处理 / 总数</dt><dd id="processed">0 / 0</dd></dl><dl class="stat"><dt>成功导出</dt><dd id="success">0</dd></dl><dl class="stat"><dt>加密待解</dt><dd id="locked">0</dd></dl><dl class="stat"><dt>失败或登录失效</dt><dd id="failed">0</dd></dl><dl class="stat"><dt>需要补充处理</dt><dd id="unsupported">0</dd></dl><dl class="stat"><dt>复用本地备份</dt><dd id="reused">0</dd></dl></div>
<div class="foot muted">最近更新时间：<span id="updated">—</span><br><span id="connection" role="status">正在读取进度</span></div></section>
<section id="external" class="panel" aria-label="外链资源进度" hidden><div class="progress-label"><h2 id="external-phase">等待外链资源处理</h2><strong id="external-count">0 / 0</strong></div><progress id="external-bar" max="100" value="0" aria-label="外链资源处理进度"></progress><div class="foot muted">已接收资源数据：<span id="external-bytes">0 MiB</span><br>外链最近更新：<span id="external-updated">—</span></div></section>
<section id="bases" class="panel" aria-label="各知识库进度" hidden><p id="scope" class="muted"></p><div id="base-cards"></div></section>
<section class="panel check" aria-label="校验摘要"><p><strong id="verification">尚无校验结果</strong></p><p id="verified" class="muted">—</p></section>
</main><script>""" + _SCRIPT + "</script></body></html>").encode("utf-8")


def _read_json(path: Path):
    try:
        with path.open("rb") as handle:
            data = handle.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            return {}
        return json.loads(data)
    except (OSError, ValueError, UnicodeError):
        # Atomic writers may not yet have published the first file. A partial
        # file from another producer must not reveal a traceback or path.
        return {}


def _read_document(path: Path) -> dict:
    document = _read_json(path)
    return document if isinstance(document, dict) else {}


def _number(value, *, default=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if isinstance(value, float) and not math.isfinite(value):
        return default
    return min(10**18, max(0, value))


def _counts(value) -> dict:
    source = value if isinstance(value, dict) else {}
    return {key: _number(source.get(key)) for key in _COUNTS}


def _external_snapshot(root: Path) -> dict | None:
    directory = Path(root) / "external-resources"
    path = directory / "progress.json"
    if directory.is_symlink() or path.is_symlink():
        return None
    directory_fd = None
    try:
        # Fixed components and no-follow opens prevent either the directory or
        # its progress file from being redirected to another local document.
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        file_fd = os.open("progress.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        with os.fdopen(file_fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return None
            data = handle.read(1024 * 1024 + 1)
            updated = _number(info.st_mtime, default=None)
        if len(data) > 1024 * 1024:
            return None
        raw = json.loads(data)
        if not isinstance(raw, dict) or not raw:
            return None
    except (OSError, ValueError, UnicodeError):
        return None
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    phase = raw.get("phase", "unknown")
    # Final mirror summaries report result counts instead of the live cursor.
    final_count = _number(raw.get("resources_mirrored")) + _number(raw.get("resources_failed"))
    return {"phase": phase if isinstance(phase, str) and phase in _EXTERNAL_PHASES else "unknown",
            "discovered": _number(raw.get("discovered"), default=final_count),
            "completed": _number(raw.get("completed"), default=final_count),
            "network_bytes": _number(raw.get("network_bytes")), "updated": updated}


def _aggregate_external(bases: list[dict], invalid=0) -> dict | None:
    items = [base["external_progress"] for base in bases if base["external_progress"]]
    if not items:
        return None
    phases = {item["phase"] for item in items}
    phase = ("mirroring" if "mirroring" in phases else "partial" if "partial" in phases
             else "unknown" if "unknown" in phases or invalid else "waiting" if len(items) != len(bases)
             else "complete" if phases == {"complete"} else "waiting")
    return {"phase": phase, **{key: sum(item[key] for item in items)
                               for key in ("discovered", "completed", "network_bytes")},
            "updated": max((item["updated"] for item in items if item["updated"] is not None), default=None)}


def _single_snapshot(root: Path) -> dict:
    raw = _read_document(Path(root) / "progress.json")
    phase = raw.get("phase", "waiting")
    progress = {
        "phase": phase if isinstance(phase, str) and phase in _PHASES else "unknown",
        "total": _number(raw.get("total", raw.get("discovered"))),
        "completed": _number(raw.get("completed")),
        "counts": _counts(raw.get("counts")),
        "elapsed_seconds": _number(raw.get("elapsed_seconds")),
        "updated": _number(raw.get("updated"), default=None),
        "total_known": _number(raw.get("total"), default=None) is not None,
        "discovered": _number(raw.get("discovered"), default=None),
    }
    external = _external_snapshot(Path(root))
    if external and external["updated"] is not None:
        progress["updated"] = max(progress["updated"] or 0, external["updated"])
    document = _read_document(Path(root) / "verification.json")
    verification = None
    if document:
        verification = {key: _number(document.get(key)) for key in ("files_checked", "inventory_notes", "records", "warnings")}
        for key in ("integrity_ok", "all_readable"):
            value = document.get(key)
            verification[key] = value if isinstance(value, bool) else None
        for key in ("missing_files", "corrupt_files", "notes_without_record"):
            value = document.get(key)
            verification[key + "_count"] = len(value) if isinstance(value, list) else 0
        verification["statuses"] = _counts(document.get("statuses"))
        verification["time"] = _number(document.get("time"), default=None)
    return {"available": bool(raw) or external is not None, "progress": progress,
            "external_progress": external, "verification": verification}


def _safe_base(root: Path, value) -> Path | None:
    if not isinstance(value, str):
        return None
    if value != "personal" and not re.fullmatch(r"teams/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        return None
    current = root
    for component in value.split("/"):
        current = current / component
        if current.is_symlink():
            return None
    try:
        current.resolve().relative_to(root)
        for name in ("progress.json", "verification.json"):
            file = current / name
            if file.is_symlink():
                return None
            file.resolve().relative_to(root)
    except (OSError, ValueError):
        return None
    return current


def _aggregate_verification(bases: list[dict], invalid=0) -> dict | None:
    documents = [base["verification"] for base in bases if base["verification"]]
    if not documents:
        return None
    result = {key: sum(item.get(key, 0) for item in documents) for key in
              ("files_checked", "inventory_notes", "records", "warnings", "missing_files_count", "corrupt_files_count", "notes_without_record_count")}
    result["statuses"] = {key: sum(item["statuses"].get(key, 0) for item in documents) for key in _COUNTS}
    for key in ("integrity_ok", "all_readable"):
        values = [item.get(key) for item in documents]
        result[key] = False if any(value is False for value in values) else True if not invalid and len(documents) == len(bases) and all(value is True for value in values) else None
    result["time"] = max((item["time"] for item in documents if item["time"] is not None), default=None)
    result.update(verified_bases=len(documents), total_bases=len(bases) + invalid)
    return result


def _account_phase(bases: list[dict], invalid: int) -> str:
    active = {"downloading", "inventory", "inventory-crosscheck", "verifying", "packaging", "catalog", "cataloging", "retrying", "discovering", "discovering-bases", "mirroring-external"}
    running = [base["progress"] for base in bases if base["progress"]["phase"] in active]
    if running:
        return max(running, key=lambda progress: progress["updated"] or 0)["phase"]
    phases = {base["progress"]["phase"] for base in bases}
    if "auth_required" in phases:
        return "auth_required"
    if "needs_retry" in phases:
        return "needs_retry"
    if invalid:
        return "unknown"
    if not bases or phases & {"not-started", "waiting", "unknown", "paused"}:
        return "account-waiting"
    if phases and phases <= {"archived", "complete"}:
        return "archived"
    if phases and phases <= {"archived", "complete", "archived-with-gaps"}:
        return "archived-with-gaps"
    return "downloaded"


def status_snapshot(root: Path) -> dict:
    """Return safe counts for one library or an account's isolated libraries."""
    root = Path(root).resolve()
    manifest_path = root / "knowledge-bases.json"
    if not manifest_path.exists() and not manifest_path.is_symlink():
        result = _single_snapshot(root)
        result.update(mode="single", bases=[])
        return result
    manifest = _read_json(manifest_path) if not manifest_path.is_symlink() else None
    manifest_valid = isinstance(manifest, list)
    entries = manifest if manifest_valid else []
    bases, seen, invalid, teams = [], set(), 0 if manifest_valid else 1, 0
    for entry in entries:
        relative = entry.get("path") if isinstance(entry, dict) else None
        local = _safe_base(root, relative)
        if local is None or relative.lower() in seen:
            invalid += 1
            continue
        seen.add(relative.lower())
        snapshot = _single_snapshot(local)
        progress = snapshot["progress"]
        if not progress["total_known"]:
            progress["total"] = None
        if not snapshot["available"]:
            progress["phase"] = "not-started"
        if relative == "personal":
            kind, label = "personal", "个人库"
        else:
            teams += 1
            kind, label = "team", "团队库 " + str(teams)
        bases.append({"kind": kind, "label": label, **snapshot})
    progresses = [base["progress"] for base in bases]
    known_total = sum(progress["total"] for progress in progresses if progress["total_known"])
    unknown = sum(not progress["total_known"] for progress in progresses) + invalid
    aggregate = {
        "phase": _account_phase(bases, invalid), "total": known_total if not unknown and bases else None,
        "total_known": bool(bases) and not unknown, "known_total": known_total,
        "unknown_total_bases": unknown, "pending_bases": sum(not base["available"] for base in bases),
        "completed": sum(progress["completed"] for progress in progresses),
        "counts": {key: sum(progress["counts"][key] for progress in progresses) for key in _COUNTS},
        "elapsed_seconds": sum(progress["elapsed_seconds"] for progress in progresses),
        "updated": max((progress["updated"] for progress in progresses if progress["updated"] is not None), default=None),
    }
    return {"mode": "account", "available": manifest_valid, "progress": aggregate,
            "verification": _aggregate_verification(bases, invalid), "external_progress": _aggregate_external(bases, invalid),
            "bases": bases, "invalid_bases": invalid}


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, root):
        self.backup_root = Path(root).resolve()
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server_version = "WizBackupDashboard"
    sys_version = ""

    def log_message(self, format, *args):
        # URLs and arbitrary request headers have no place in backup logs.
        pass

    def _reply(self, code, body, content_type="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", _CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        port = self.server.server_port
        if self.headers.get("Host", "") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self._reply(403, b"Forbidden\n")
            return
        if self.path == "/":
            self._reply(200, _PAGE, "text/html; charset=utf-8")
        elif self.path == "/status":
            data = json.dumps(status_snapshot(self.server.backup_root), ensure_ascii=False, allow_nan=False).encode("utf-8")
            self._reply(200, data, "application/json; charset=utf-8")
        else:
            self._reply(404, b"Not found\n")

    def do_POST(self):
        self._reply(405, b"Method not allowed\n")

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_POST


def create_server(root: Path, host="127.0.0.1", port=8765) -> ThreadingHTTPServer:
    """Create an unstarted server; port=0 is useful for isolated tests."""
    if host != "127.0.0.1":
        raise ValueError("The progress dashboard may bind only to 127.0.0.1")
    return _DashboardServer((host, port), root)


def serve(root: Path, host="127.0.0.1", port=8765):
    """Block while serving the loopback dashboard; close cleanly on Ctrl-C."""
    server = create_server(root, host=host, port=port)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
