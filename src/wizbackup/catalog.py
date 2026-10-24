"""Offline catalog and integrity-aware rewriting of Wiz document links."""
from __future__ import annotations

import base64
from collections import Counter
import copy
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
from urllib.parse import parse_qs, quote, unquote, urlsplit

_SCRIPT = """'use strict';
const search = document.getElementById('search');
const rows = Array.from(document.querySelectorAll('.note'));
const groups = Array.from(document.querySelectorAll('.group'));
let selected = '';
function filter() {
  const query = search.value.trim().toLocaleLowerCase();
  let visible = 0;
  for (const row of rows) {
    const matchesFolder = !selected || row.dataset.category === selected || row.dataset.category.startsWith(selected.replace(/\\/$/, '') + '/');
    row.hidden = !(matchesFolder && row.dataset.title.toLocaleLowerCase().includes(query));
    if (!row.hidden) visible += 1;
  }
  for (const group of groups) group.hidden = !Array.from(group.querySelectorAll('.note')).some(row => !row.hidden);
  document.getElementById('visible-count').textContent = visible.toLocaleString('zh-CN');
  document.getElementById('empty').hidden = visible !== 0;
}
search.addEventListener('input', filter);
for (const button of document.querySelectorAll('.folder')) {
  button.addEventListener('click', () => {
    selected = button.dataset.category;
    for (const other of document.querySelectorAll('.folder')) other.setAttribute('aria-pressed', String(other === button));
    filter();
  });
}
filter();
"""
_SCRIPT_HASH = base64.b64encode(hashlib.sha256(_SCRIPT.encode()).digest()).decode()
_CSP = ("default-src 'none'; script-src 'sha256-" + _SCRIPT_HASH + "'; "
        "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'; connect-src 'none'; object-src 'none'")
_STYLE = """
:root{color-scheme:light dark;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}*{box-sizing:border-box}body{margin:0;background:#f4f6f8;color:#172330}header{padding:28px 32px;background:white;border-bottom:1px solid #dbe3ea}h1{margin:0 0 10px;font-size:26px}.muted{color:#637282}input{width:min(100%,680px);font:inherit;padding:12px 14px;border:1px solid #b8c5d0;border-radius:8px;background:transparent;color:inherit}.layout{display:grid;grid-template-columns:260px 1fr;min-height:calc(100vh - 160px)}nav{padding:22px 14px;border-right:1px solid #dbe3ea;overflow:auto}nav h2{font-size:15px;margin:0 12px 14px}.folder{display:flex;justify-content:space-between;align-items:center;gap:8px;width:100%;padding:9px 10px;padding-left:calc(10px + var(--depth,0)*14px);border:0;border-radius:6px;text-align:left;background:transparent;color:inherit;cursor:pointer;overflow-wrap:anywhere}.folder[aria-pressed=true]{background:#dcebf4;font-weight:600}.folder span{color:#637282;font-size:12px;flex-shrink:0}main{padding:24px 32px;max-width:1400px;min-width:0}.group{margin:0 0 26px}.group h2{font-size:17px;color:#53697c;margin:0 0 12px;overflow-wrap:anywhere}.notes{list-style:none;padding:0;margin:0;background:white;border:1px solid #dbe3ea;border-radius:10px}.note{padding:14px 16px;border-bottom:1px solid #e5ebef;overflow-wrap:anywhere}.note:last-child{border:0}.note a{color:#12618c;text-decoration:none}.note a:hover{text-decoration:underline}.note .format{font-size:12px;margin-left:12px;white-space:nowrap}.status{font-size:12px;color:#637282;margin-left:12px}.note[hidden],.group[hidden],[hidden]{display:none!important}.summary{margin:14px 0 0;font-size:14px}@media(max-width:700px){header{padding:22px 18px}.layout{grid-template-columns:1fr}nav{border-right:0;border-bottom:1px solid #dbe3ea;max-height:240px}main{padding:24px 18px}h1{font-size:23px}}@media(prefers-color-scheme:dark){body{background:#141b22;color:#e7edf4}header,.notes{background:#1b2631;border-color:#344352}nav,.note{border-color:#344352}.muted,.status,.folder span,.group h2{color:#b4c1ce}.folder[aria-pressed=true]{background:#29475c}.note a{color:#8fc9ef}input{border-color:#53697c}}
"""
_URL = r"(?:wiz:(?://)?open_document[^\s<>\"'\]\)]*|https?://[^\s<>\"'\]\)]*)"
_URL_RE = re.compile(r"<(?P<auto>" + _URL + r")>|(?P<url>" + _URL + r")", re.I)
# Recognize relative/external destinations as well, so a URI displayed in a
# link's label is never rewritten as prose on the second catalog pass.
_MD_LINK_RE = re.compile(r"(?P<lead>!?\[[^\]\n]*\]\(\s*)(?P<angle><)?(?P<url>[^\s<>)]*)(?P<close>>)?(?P<tail>\s*(?:[\"'][^\"'\n]*[\"']\s*)?\))", re.I)
_HTML_LINK_RE = re.compile(r"(?P<lead><a\b[^>]*?\bhref\s*=\s*)(?P<quote>[\"'])(?P<url>.*?)(?P=quote)", re.I | re.S)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    part = path.with_name(path.name + ".part")
    with part.open("wb") as handle:
        os.chmod(part, 0o600)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    part.replace(path)


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _path(root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Invalid relative path")
    posix = PurePosixPath(value)
    if posix.is_absolute() or any(p in {".", ".."} for p in value.split("/")):
        raise ValueError("Unsafe relative path")
    target = root / Path(*posix.parts)
    target.resolve().relative_to(root)
    candidate = root
    for component in posix.parts:
        candidate = candidate / component
        if candidate.is_symlink():
            raise ValueError("Symbolic links are not reading copies")
    return target


def _document_id(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(html.unescape(url))
    except ValueError:
        return None
    guid = None
    if parts.scheme.lower() == "wiz":
        command = (parts.netloc or parts.path).strip("/").lower()
        if command != "open_document":
            return None
        query = {key.lower(): value for key, value in parse_qs(parts.query).items()}
        for key in ("guid", "docguid", "documentguid", "document_guid"):
            if query.get(key):
                guid = query[key][0]
                break
    elif parts.scheme.lower() in {"http", "https"}:
        host = (parts.hostname or "").lower()
        if host != "wiz.cn" and not host.endswith(".wiz.cn"):
            return None
        match = re.fullmatch(r"/ks/note/view/[^/]+/([^/]+)/?", parts.path, flags=re.I)
        if match is None:
            match = re.fullmatch(r"/note-plus/note/[^/]+/([^/]+)/?", parts.path, flags=re.I)
        if match:
            guid = unquote(match.group(1))
    if not guid:
        return None
    return guid.strip("{} ").lower(), parts.fragment


def _local_link(url: str, source: Path, root: Path, catalog: dict, kind: str, stats: Counter) -> str:
    document = _document_id(url)
    if document is None:
        return url
    guid, fragment = document
    entry = catalog.get(guid)
    if not entry or not entry.get(kind):
        stats["unresolved_links"] += 1
        return url
    target = _path(root, entry[kind])
    relative = os.path.relpath(target, source.parent).replace(os.sep, "/")
    result = quote(relative, safe="/")
    if fragment:
        result += "#" + quote(fragment, safe="-._~!$&'()*+,;=:@/?%")
    if result != url:
        stats["rewritten_links"] += 1
    return result


def _md_inline(value: str, source: Path, root: Path, catalog: dict, stats: Counter) -> str:
    def bare(segment):
        def replace(match):
            original = match.group("auto") or match.group("url")
            target = _local_link(original, source, root, catalog, "md", stats)
            if target == original:
                return match.group()
            # CommonMark autolinks require a URI scheme; turn an internal
            # autolink into a regular relative link after resolving it.
            return "[" + original + "](" + target + ")" if match.group("auto") else target
        return _URL_RE.sub(replace, segment)
    output = []
    start = 0
    for match in _MD_LINK_RE.finditer(value):
        output.append(bare(value[start:match.start()]))
        target = _local_link(match.group("url"), source, root, catalog, "md", stats)
        output.append(match.group("lead") + (match.group("angle") or "") + target + (match.group("close") or "") + match.group("tail"))
        start = match.end()
    output.append(bare(value[start:]))
    return "".join(output)


def _markdown_links(body: str, source: Path, root: Path, catalog: dict, stats: Counter) -> str:
    lines = body.splitlines(keepends=True)
    output = []
    frontmatter = bool(lines and lines[0].strip() == "---")
    fence = None
    for index, line in enumerate(lines):
        if frontmatter:
            output.append(line)
            if index > 0 and line.strip() in {"---", "..."}:
                frontmatter = False
            continue
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            value = marker.group(1)
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
            output.append(line)
            continue
        if fence is not None or line.startswith(("    ", "\t")):
            output.append(line)
            continue
        chunks = re.split(r"(`+.*?`+)", line)
        output.append("".join(chunk if i % 2 else _md_inline(chunk, source, root, catalog, stats) for i, chunk in enumerate(chunks)))
    return "".join(output)


def _html_links(body: str, source: Path, root: Path, catalog: dict, stats: Counter) -> str:
    def replace(match):
        target = _local_link(match.group("url"), source, root, catalog, "html", stats)
        if target == match.group("url"):
            return match.group()
        return match.group("lead") + match.group("quote") + html.escape(target, quote=True) + match.group("quote")
    return _HTML_LINK_RE.sub(replace, body)


def _categories(notes: dict, additional=()) -> list[str]:
    categories = {"/"}
    values = [entry["category"] for entry in notes.values()] + [value for value in additional if isinstance(value, str)]
    for value in values:
        parts = [part for part in value.split("/") if part]
        for index in range(1, len(parts) + 1):
            categories.add("/" + "/".join(parts[:index]) + "/")
    return sorted(categories, key=lambda value: (value.casefold(), value))


def _page(catalog: dict, additional_categories=()) -> bytes:
    categories = _categories(catalog, additional_categories)
    folders = ['<button type="button" class="folder" data-category="" aria-pressed="true">全部笔记<span>' + str(len(catalog)) + "</span></button>"]
    for category in categories:
        count = sum(entry["category"] == category or entry["category"].startswith(category) for entry in catalog.values())
        depth = max(0, len([part for part in category.split("/") if part]) - 1)
        label = category.rstrip("/").split("/")[-1] if category != "/" else "根目录"
        folders.append(f'<button type="button" class="folder" data-category="{html.escape(category, quote=True)}" aria-pressed="false" style="--depth:{depth}">{html.escape(label)}<span>{count}</span></button>')
    groups = []
    for category in sorted({entry["category"] for entry in catalog.values()}, key=str.casefold):
        rows = []
        entries = sorted((entry for entry in catalog.values() if entry["category"] == category), key=lambda e: (e["title"].casefold(), e["guid"]))
        for entry in entries:
            title = html.escape(entry["title"])
            primary = entry.get("html") or entry.get("md")
            label = f'<a href="{html.escape(quote(primary, safe="/"), quote=True)}">{title}</a>' if primary else title
            md = f'<a class="format" href="{html.escape(quote(entry["md"], safe="/"), quote=True)}">Markdown</a>' if entry.get("md") else ""
            state = {"locked": "加密待解", "unsupported": "待转换", "failed": "下载待重试", "auth_required": "登录已失效", "missing": "尚未备份"}.get(entry["status"], "")
            status = '<span class="status">' + state + "</span>" if state else ""
            rows.append(f'<li class="note" data-category="{html.escape(category, quote=True)}" data-title="{html.escape(entry["title"], quote=True)}">{label}{md}{status}</li>')
        groups.append('<section class="group"><h2>' + html.escape(category) + '</h2><ul class="notes">' + "".join(rows) + "</ul></section>")
    page = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="' + html.escape(_CSP, quote=True) + '"><meta name="referrer" content="no-referrer">'
            '<title>为知笔记本地知识库</title><style>' + _STYLE + '</style></head><body><header><h1>为知笔记本地知识库</h1>'
            '<input id="search" type="search" aria-label="搜索笔记标题" placeholder="搜索笔记标题…"><p class="summary muted">显示 <span id="visible-count">' + str(len(catalog)) + '</span> 篇笔记 · 本地离线目录</p></header>'
            '<div class="layout"><nav aria-label="笔记目录"><h2>目录</h2>' + "".join(folders) + '</nav><main>' + "".join(groups) + '<p id="empty" class="muted" hidden>没有匹配的笔记。</p></main></div><script>' + _SCRIPT + '</script></body></html>')
    return page.encode("utf-8")


def finish_catalog(root: Path, *, apply_wiz_links=True) -> dict:
    """Build local navigation and rewrite links without blessing damaged files.

    Affected records are checked against *all* their existing file checksums
    before any reading copy is changed. Only already-registered reading copies
    receive new checksum values. Any integrity problem aborts the whole batch.
    """
    root = Path(root).resolve()
    result = {"ok": False, "notes": 0, "readable_notes": 0, "rewritten_files": 0,
              "rewritten_links": 0, "updated_records": 0, "unresolved_links": 0, "problems": []}
    problems = result["problems"]
    try:
        inventory = json.loads((root / "metadata/notes.json").read_text(encoding="utf-8"))
        if not isinstance(inventory, list):
            raise ValueError("Inventory must be a list")
        additional_categories = []
        category_file = root / "metadata/categories.json"
        if category_file.is_file():
            category_data = json.loads(category_file.read_text(encoding="utf-8"))
            values = category_data.get("result", []) if isinstance(category_data, dict) else category_data
            if isinstance(values, list):
                additional_categories = values
        records = {}
        for path in sorted((root / "records").glob("*.json")):
            path = _path(root, path.relative_to(root).as_posix())
            raw = path.read_bytes()
            record = json.loads(raw)
            guid = str(record["guid"]).lower()
            if guid in records:
                raise ValueError("Duplicate record GUID")
            records[guid] = {"path": path, "raw": raw, "record": record}
        catalog = {}
        for note in inventory:
            guid = str(note["docGuid"]).strip("{}").lower()
            if guid in catalog:
                raise ValueError("Duplicate inventory GUID")
            category_parts = [part for part in str(note.get("category", "/")).split("/") if part]
            category = "/" + "/".join(category_parts) + "/" if category_parts else "/"
            record = records.get(guid, {}).get("record", {})
            status = record.get("status", "missing")
            entry = {"guid": guid, "title": str(note.get("title", "Untitled")), "category": category,
                     "md": None, "html": None, "status": status if isinstance(status, str) else "unknown"}
            relative = record.get("readable_path")
            if relative:
                base = _path(root, relative)
                if PurePosixPath(relative).parts[0] != "readable":
                    raise ValueError("Reading copy is outside readable directory")
                for kind, filename in (("md", "note.md"), ("html", "index.html")):
                    target = base / filename
                    target.resolve().relative_to(root)
                    if target.is_file() and not target.is_symlink():
                        entry[kind] = target.relative_to(root).as_posix()
            catalog[guid] = entry
        result["notes"] = len(catalog)
        result["readable_notes"] = sum(bool(entry["md"] or entry["html"]) for entry in catalog.values())
        plans = {}
        affected = set()
        stats = Counter()
        if apply_wiz_links:
            for guid, entry in catalog.items():
                for kind in ("md", "html"):
                    if not entry[kind]:
                        continue
                    source = _path(root, entry[kind])
                    old_bytes = source.read_bytes()
                    body = old_bytes.decode("utf-8")
                    rewritten = (_markdown_links if kind == "md" else _html_links)(body, source, root, catalog, stats)
                    new_bytes = rewritten.encode("utf-8")
                    if new_bytes != old_bytes:
                        files = records[guid]["record"].get("files", [])
                        matches = [item for item in files if item.get("path") == entry[kind]]
                        if len(matches) != 1:
                            problems.append({"kind": "reading_copy_not_uniquely_registered", "path": entry[kind]})
                            continue
                        if entry[kind] in plans:
                            problems.append({"kind": "reading_copy_shared_between_records", "path": entry[kind]})
                            continue
                        plans[entry[kind]] = {"path": source, "before": old_bytes, "after": new_bytes, "guid": guid}
                        affected.add(guid)
        # Validate every old checksum for affected records before publishing any
        # new checksum. A damaged raw ZIP must never be silently re-approved.
        for guid in sorted(affected):
            saved = records[guid]
            if saved["path"].read_bytes() != saved["raw"]:
                problems.append({"kind": "record_changed_concurrently", "guid": guid})
            for item in saved["record"].get("files", []):
                relative = item.get("path", "")
                try:
                    path = _path(root, relative)
                    if not path.is_file():
                        problems.append({"kind": "missing_file", "path": relative})
                    elif path.stat().st_size != item.get("size") or _digest(path) != item.get("sha256"):
                        problems.append({"kind": "checksum_mismatch", "path": relative})
                except (OSError, ValueError):
                    problems.append({"kind": "unsafe_or_unreadable_file", "path": relative})
        for relative, plan in plans.items():
            if plan["path"].read_bytes() != plan["before"]:
                problems.append({"kind": "reading_copy_changed_concurrently", "path": relative})
        if problems:
            return result
        for plan in plans.values():
            _atomic(plan["path"], plan["after"])
            result["rewritten_files"] += 1
        for guid in sorted(affected):
            record = copy.deepcopy(records[guid]["record"])
            for item in record.get("files", []):
                plan = plans.get(item.get("path"))
                if plan and plan["guid"] == guid:
                    item["size"] = len(plan["after"])
                    item["sha256"] = hashlib.sha256(plan["after"]).hexdigest()
            _atomic(records[guid]["path"], _json_bytes(record))
            result["updated_records"] += 1
        _atomic(root / "catalog.json", _json_bytes(catalog))
        _atomic(root / "index.html", _page(catalog, additional_categories))
        result["rewritten_links"] = stats["rewritten_links"]
        result["unresolved_links"] = stats["unresolved_links"]
        result["ok"] = True
    except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
        # Deliberately omit exception text, which can contain document content.
        problems.append({"kind": "catalog_error", "error_type": type(error).__name__})
    return result
