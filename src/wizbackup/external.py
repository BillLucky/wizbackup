"""Optional, credential-free mirroring of third-party reading-copy resources.

Original notes, attachments, and their extracted original HTML are never edited.
URLs are retained only in the local manifest; public results contain counts.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import ipaddress
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import threading
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import requests

from .client import ApiError, atomic_bytes, write_json, normalized_proxy

MAX_RESOURCE_BYTES = 50 * 1024**2
MAX_TOTAL_BYTES = 2 * 1024**3
MAX_RESOURCES = 5000
MAX_CSS_REFERENCES = 200
MAX_CSS_DEPTH = 2
_HINTS = {"dns-prefetch", "preconnect", "prefetch", "prerender", "alternate", "search"}
_CSS_URL = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)
_CSS_IMPORT = re.compile(r"(@import\s+)(['\"])(.*?)\2", re.I)


class MirrorError(RuntimeError):
    """Controlled error with no URL, credentials, or response body."""


def _digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical(url):
    p = urlsplit(url.strip())
    if not p.scheme and p.netloc:
        p = p._replace(scheme="https")
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def _remote(url):
    return isinstance(url, str) and urlsplit(url.strip()).scheme in ("http", "https") or (
        isinstance(url, str) and url.strip().startswith("//"))


def _validated(url):
    try:
        p = urlsplit(url)
        host = (p.hostname or "").rstrip(".").lower()
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        raise MirrorError("InvalidURL") from None
    if p.scheme not in ("http", "https") or not host or p.username or p.password or port not in (80, 443):
        raise MirrorError("InvalidURL")
    if host == "wiz.cn" or host.endswith(".wiz.cn") or host.endswith(".aliyuncs.com"):
        raise MirrorError("UnmappedWizResource")
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0].split("%", 1)[0]).is_global for row in addresses):
            raise MirrorError("PrivateAddressBlocked")
    except (socket.gaierror, ValueError):
        raise MirrorError("DNSResolutionFailed") from None
    return host


def _contained(root, relative):
    p = (root / relative).resolve()
    if root not in p.parents:
        raise MirrorError("PathOutsideBackup")
    return p


def _cache_file(root, url, relative, *, source=False):
    """Cache metadata may never redirect a write into original note files."""
    key = hashlib.sha256(url.encode()).hexdigest()
    pattern = rf"external-resources/{key}\.source\.css" if source else rf"external-resources/{key}\.[a-z0-9]{{1,8}}"
    if not isinstance(relative, str) or not re.fullmatch(pattern, relative):
        raise MirrorError("InvalidCachePath")
    cache = root / "external-resources"
    path = root / relative
    if cache.is_symlink() or path.is_symlink():
        raise MirrorError("CacheSymlinkBlocked")
    resolved = _contained(root, relative)
    if resolved.parent != cache or resolved.name != path.name:
        raise MirrorError("InvalidCachePath")
    return resolved


def _write_cache(root, url, path, data, *, source=False):
    path = _cache_file(root, url, str(path.relative_to(root)), source=source)
    part = path.with_name(path.name + ".part")
    if part.is_symlink() or part.exists() and not part.is_file():
        raise MirrorError("UnsafeCacheStagingPath")
    atomic_bytes(path, data)


def _write_control(path, value):
    part = path.with_name(path.name + ".part")
    if path.is_symlink() or part.is_symlink() or part.exists() and not part.is_file():
        raise MirrorError("UnsafeCacheControlPath")
    write_json(path, value)


def _cache_entry(root, url, entry):
    if not isinstance(url, str) or not isinstance(entry, dict) or entry.get("status") not in ("downloaded", "failed"):
        raise MirrorError("InvalidCacheEntrySchema")
    for value in (url, entry.get("final_url", url)):
        if not isinstance(value, str):
            raise MirrorError("InvalidCacheEntrySchema")
        try:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 80, 443):
                raise MirrorError("InvalidCacheEntryURL")
        except ValueError:
            raise MirrorError("InvalidCacheEntryURL") from None
    if entry["status"] == "downloaded":
        if (not isinstance(entry.get("size"), int) or isinstance(entry.get("size"), bool) or entry["size"] < 0
                or not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])):
            raise MirrorError("InvalidCacheEntrySchema")
        _cache_file(root, url, entry.get("path"))
        if entry.get("source_path"):
            _cache_file(root, url, entry["source_path"], source=True)
            if not str(entry["path"]).endswith(".css") or not isinstance(entry.get("source_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", entry["source_sha256"]):
                raise MirrorError("InvalidCacheEntrySchema")
    for key in ("dependencies", "unresolved_dependencies"):
        if key in entry and (not isinstance(entry[key], list) or not all(isinstance(x, str) for x in entry[key])):
            raise MirrorError("InvalidCacheEntrySchema")
    return entry


def _checked_files(root, record):
    if not record.get("files"):
        return False
    try:
        return all((p := _contained(root, e["path"])).is_file()
                   and p.stat().st_size == e["size"] and _digest(p) == e["sha256"]
                   for e in record["files"])
    except (OSError, ValueError, KeyError, TypeError, MirrorError):
        return False


def _extension(url, content_type, role):
    if role == "css" or content_type == "text/css":
        return ".css"
    known = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
             "image/svg+xml": ".svg", "image/avif": ".avif", "image/x-icon": ".ico",
             "font/woff": ".woff", "font/woff2": ".woff2", "font/ttf": ".ttf", "font/otf": ".otf"}
    ext = known.get(content_type) or Path(urlsplit(url).path).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", ext or ""):
        ext = mimetypes.guess_extension(content_type) or ".bin"
    return ext if ext not in (".html", ".htm", ".js", ".exe") else ".bin"


def _image_signature(data):
    prefix = bytes(data[:2048])
    return (prefix.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM",
                               b"\x00\x00\x01\x00", b"II*\x00", b"MM\x00*"))
            or prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP"
            or prefix[4:8] == b"ftyp" and any(brand in prefix[:64] for brand in (b"avif", b"avis", b"heic", b"heif"))
            or b"<svg" in prefix.lstrip().lower())


class _Fetcher:
    def __init__(self, root, proxy, prior):
        self.root, self.proxy, self.prior = root, proxy, prior
        self.entries = {}
        self.lock = threading.Lock()
        self.bytes_received = 0
        self.started = 0

    def failure(self, url, error):
        return {"status": "failed", "host": urlsplit(url).hostname or "", "error": error}

    def fetch(self, url, role):
        if url in self.entries:
            return self.entries[url]
        old = self.prior.get(url, {})
        if old.get("status") == "downloaded":
            try:
                _cache_entry(self.root, url, old)
                p = _cache_file(self.root, url, old["path"])
                valid = p.is_file() and p.stat().st_size == old["size"] and _digest(p) == old["sha256"]
                if old.get("source_path"):
                    source = _cache_file(self.root, url, old["source_path"], source=True)
                    valid = valid and source.is_file() and _digest(source) == old["source_sha256"]
                if valid:
                    result = dict(old)
                    result["cached"] = True
                    self.entries[url] = result
                    return result
            except (OSError, KeyError, MirrorError):
                pass
        with self.lock:
            self.started += 1
            if self.started > MAX_RESOURCES:
                result = self.failure(url, "ResourceCountLimit")
                self.entries[url] = result
                return result
        result = None
        for _attempt in range(2):
            session = requests.Session()
            session.trust_env = False
            session.proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else {}
            session.headers.clear()
            session.auth = None
            try:
                current = url
                for _redirect in range(6):
                    _validated(current)
                    session.cookies.clear()
                    with session.get(current, stream=True, allow_redirects=False, timeout=(10, 10),
                                     headers={"User-Agent": "wizbackup-offline/0.1", "Accept": "*/*"}) as response:
                        if response.is_redirect:
                            location = response.headers.get("Location")
                            if not location:
                                raise MirrorError("MissingRedirectLocation")
                            current = _canonical(urljoin(current, location))
                            continue
                        if response.status_code >= 400:
                            raise MirrorError(f"HTTP{response.status_code}")
                        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower().strip()
                        if content_type in ("text/html", "application/xhtml+xml", "application/javascript", "text/javascript"):
                            raise MirrorError("UnexpectedContentType")
                        if role == "image" and content_type and not content_type.startswith("image/") and content_type != "application/octet-stream":
                            raise MirrorError("UnexpectedContentType")
                        if role == "css" and content_type not in ("", "text/css", "text/plain", "application/octet-stream"):
                            raise MirrorError("UnexpectedContentType")
                        length = response.headers.get("Content-Length")
                        if length and int(length) > MAX_RESOURCE_BYTES:
                            raise MirrorError("ResourceSizeLimit")
                        data = bytearray()
                        for chunk in response.iter_content(64 * 1024):
                            if not chunk:
                                continue
                            with self.lock:
                                self.bytes_received += len(chunk)
                                if self.bytes_received > MAX_TOTAL_BYTES:
                                    raise MirrorError("TotalSizeLimit")
                            data.extend(chunk)
                            if len(data) > MAX_RESOURCE_BYTES:
                                raise MirrorError("ResourceSizeLimit")
                        if not data:
                            raise MirrorError("EmptyResource")
                        if length and not response.headers.get("Content-Encoding") and len(data) != int(length):
                            raise MirrorError("TruncatedResource")
                        if bytes(data[:256]).lstrip().lower().startswith((b"<!doctype html", b"<html")):
                            raise MirrorError("UnexpectedHTMLBody")
                        if role == "image" and not _image_signature(data):
                            raise MirrorError("InvalidImageSignature")
                        ext = _extension(current, content_type, role)
                        key = hashlib.sha256(url.encode()).hexdigest()
                        target = self.root / "external-resources" / (key + ext)
                        _write_cache(self.root, url, target, bytes(data))
                        result = {"status": "downloaded", "host": urlsplit(current).hostname,
                                  "final_url": current, "path": str(target.relative_to(self.root)),
                                  "size": len(data), "sha256": _digest(target), "content_type": content_type,
                                  "role": role, "cached": False}
                        if ext == ".css":
                            source = target.with_name(key + ".source.css")
                            _write_cache(self.root, url, source, bytes(data), source=True)
                            result.update(source_path=str(source.relative_to(self.root)), source_sha256=_digest(source))
                        break
                else:
                    raise MirrorError("RedirectLimit")
                break
            except (requests.RequestException, OSError, ValueError, MirrorError) as error:
                result = self.failure(url, str(error) if isinstance(error, MirrorError) else type(error).__name__)
            finally:
                session.close()
        self.entries[url] = result
        return result

    def css(self, url, depth=0, stack=()):
        entry = self.entries[url]
        if entry.get("status") != "downloaded" or not entry.get("source_path"):
            return
        if url in stack:
            return
        source = _cache_file(self.root, url, entry["source_path"], source=True)
        text = source.read_bytes().decode("utf-8-sig", errors="replace")
        dependencies, unresolved, references = set(), set(), [0]
        base = entry.get("final_url", url)

        def resolve(value, importing=False):
            if not value or value.startswith(("data:", "#")):
                return value
            absolute = _canonical(urljoin(base, value))
            references[0] += 1
            if references[0] > MAX_CSS_REFERENCES or depth >= MAX_CSS_DEPTH:
                unresolved.add(absolute)
                self.entries.setdefault(absolute, self.failure(absolute, "CSSDepthOrReferenceLimit"))
                return absolute
            kind = "css" if importing or urlsplit(absolute).path.lower().endswith(".css") else "asset"
            dependency = self.fetch(absolute, kind)
            dependencies.add(absolute)
            if dependency.get("status") != "downloaded":
                unresolved.add(absolute)
                return absolute
            if dependency.get("source_path"):
                self.css(absolute, depth + 1, (*stack, url))
                unresolved.update(dependency.get("unresolved_dependencies", []))
            local = _contained(self.root, dependency["path"])
            return quote(os.path.relpath(local, _contained(self.root, entry["path"]).parent), safe="/")

        # Handle quoted imports separately; URL imports are handled by url().
        text = _CSS_IMPORT.sub(lambda m: m[1] + '"' + resolve(m[3], importing=True) + '"', text)
        text = _CSS_URL.sub(lambda m: 'url("' + resolve(m[2]).replace('"', "%22") + '")', text)
        target = _cache_file(self.root, url, entry["path"])
        _write_cache(self.root, url, target, text.encode())
        entry.update(size=target.stat().st_size, sha256=_digest(target), dependencies=sorted(dependencies),
                     unresolved_dependencies=sorted(unresolved))


def _document_plan(root, record_path, rec, prior):
    dest = _contained(root, rec["readable_path"])
    if root / "readable" not in dest.parents:
        raise MirrorError("NotAReadingCopy")
    path = dest / "index.html"
    soup = BeautifulSoup(path.read_text(), "html.parser")
    urls, ignored, html_refs = {}, [], []
    cached_css = {entry.get("path"): url for url, entry in prior.items() if entry.get("source_path")}
    for node in list(soup.find_all(True)):
        if node.name == "link":
            rel = set(node.get("rel", []))
            if rel & _HINTS or any("icon" in x for x in rel):
                value = node.get("href")
                if _remote(value):
                    ignored.append({"url": _canonical(value), "reason": "non_content_link"})
                    node.decompose()
                continue
        for attr in ("src", "poster", "background", "href"):
            value = node.get(attr)
            if (isinstance(value, str) and not _remote(value) and node.name == "link"
                    and attr == "href" and "stylesheet" in node.get("rel", [])):
                # A previous partial CSS mirror must retry its unresolved
                # dependencies even after the stylesheet href became local.
                candidate = (dest / value).resolve()
                if root in candidate.parents:
                    previous = cached_css.get(str(candidate.relative_to(root)))
                    if previous:
                        urls[previous] = "css"
                        html_refs.append((node, attr, previous))
            if not _remote(value):
                continue
            role = None
            if node.name == "img" and attr == "src" or attr in ("poster", "background"):
                role = "image"
            elif node.name == "link" and attr == "href" and "stylesheet" in node.get("rel", []):
                role = "css"
            if role:
                url = _canonical(value)
                urls[url] = role
                html_refs.append((node, attr, url))
        css = node.get("style")
        if isinstance(css, str):
            for match in _CSS_URL.finditer(css):
                if _remote(match[2]):
                    urls[_canonical(match[2])] = "asset"
    for style in soup.find_all("style"):
        if style.string:
            for match in _CSS_URL.finditer(style.string):
                if _remote(match[2]):
                    urls[_canonical(match[2])] = "asset"
    markdown = dest / "note.md"
    if markdown.is_file():
        text = markdown.read_text()
        if text.startswith("---\n"):
            parts = text.split("\n---\n", 1)
            text = parts[1] if len(parts) == 2 else ""
        fence = None
        for line in text.splitlines():
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if marker:
                value = marker[1]
                fence = None if fence and value[0] == fence[0] and len(value) >= len(fence) else value
                continue
            if fence or line.startswith(("    ", "\t")):
                continue
            for i, chunk in enumerate(re.split(r"(`+.*?`+)", line)):
                if i % 2:
                    continue
                for match in re.finditer(r'!\[[^\]\n]*\]\(([^\s()]+)', chunk):
                    if _remote(match[1]):
                        urls[_canonical(match[1])] = "image"
    return {"record_path": record_path, "record": rec, "dest": dest, "html_path": path,
            "soup": soup, "urls": urls, "refs": html_refs, "ignored": ignored}


def mirror_external(root: Path, proxy=None, workers=3) -> dict:
    """Mirror public content resources, then update only verified reading copies.

    Call after the main backup has stopped writing this destination. Failed URLs
    remain in local manifests and export diagnostics; no resource is invented.
    """
    try: proxy = normalized_proxy(proxy)
    except ApiError: raise MirrorError("InvalidProxyConfiguration") from None
    root = Path(root).resolve()
    cache = root / "external-resources"
    if cache.is_symlink():
        raise MirrorError("CacheSymlinkBlocked")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache.chmod(0o700)
    manifest_path = cache / "manifest.json"
    prior, schema_errors = {}, []
    try:
        if manifest_path.is_symlink():
            raise MirrorError("UnsafeCacheControlPath")
        saved = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        if not isinstance(saved, dict):
            raise MirrorError("ManifestMustBeObject")
        resources = saved.get("resources", {})
        if not isinstance(resources, dict):
            raise MirrorError("ManifestResourcesMustBeObject")
        for url, entry in resources.items():
            try:
                prior[url] = _cache_entry(root, url, entry)
            except (MirrorError, ValueError, TypeError):
                schema_errors.append({"url_hash": hashlib.sha256(str(url).encode()).hexdigest(),
                                      "error": "InvalidCacheEntrySchemaOrPath"})
    except (OSError, ValueError, MirrorError) as error:
        schema_errors.append({"error": str(error) if isinstance(error, MirrorError) else type(error).__name__})
    plans, roots, skipped = [], {}, []
    for record_path in sorted((root / "records").glob("*.json")):
        try:
            rec = json.loads(record_path.read_text())
            if rec.get("status") != "exported" or not rec.get("readable_path"):
                continue
            if not _checked_files(root, rec):
                skipped.append({"record": record_path.name, "error": "RecordIntegrityFailed"})
                continue
            plan = _document_plan(root, record_path, rec, prior)
            roots.update(plan["urls"])
            # Retain URL inventories, not thousands of BeautifulSoup trees.
            # Reparse one verified reading copy at a time after networking.
            plans.append({key: plan[key] for key in ("record_path", "record", "urls", "ignored")})
        except (OSError, ValueError, KeyError, MirrorError):
            skipped.append({"record": record_path.name, "error": "InvalidReadingRecord"})
    fetcher = _Fetcher(root, proxy, prior)
    progress_path = cache / "progress.json"
    _write_control(progress_path, {"phase": "mirroring", "discovered": len(roots), "completed": 0})
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 3))) as pool:
        futures = [pool.submit(fetcher.fetch, url, role) for url, role in roots.items()]
        for future in as_completed(futures):
            future.result()
            completed += 1
            _write_control(progress_path, {"phase": "mirroring", "discovered": len(roots),
                                      "completed": completed, "network_bytes": fetcher.bytes_received})
    for url in list(roots):
        fetcher.css(url)
    updated = 0
    for saved in plans:
        # The cache may legitimately have changed while CSS dependencies were
        # repaired. Reading copies and original note files must still match the
        # pre-download record before any new checksums can be committed.
        unchanged = {**saved["record"], "files": [entry for entry in saved["record"]["files"]
                     if not entry["path"].startswith("external-resources/")]}
        if not _checked_files(root, unchanged):
            skipped.append({"record": saved["record_path"].name, "error": "RecordChangedDuringMirror"})
            continue
        plan = _document_plan(root, saved["record_path"], saved["record"], prior)
        rec, dest = plan["record"], plan["dest"]
        used, failed = set(), set()

        def local_url(url):
            url = _canonical(url)
            entry = fetcher.entries.get(url, {})
            if entry.get("status") != "downloaded":
                failed.add(url)
                return None
            used.add(url)
            failed.update(entry.get("unresolved_dependencies", []))
            return quote(os.path.relpath(_contained(root, entry["path"]), dest), safe="/")

        for node, attr, url in plan["refs"]:
            value = local_url(url)
            if value is not None:
                node[attr] = value

        def inline_css(text):
            def replace(m):
                if not _remote(m[2]):
                    return m[0]
                local = local_url(m[2])
                return 'url("' + local + '")' if local else m[0]
            return _CSS_URL.sub(replace, text)

        for node in plan["soup"].find_all(True):
            if isinstance(node.get("style"), str):
                node["style"] = inline_css(node["style"])
        for style in plan["soup"].find_all("style"):
            if style.string:
                style.string.replace_with(inline_css(style.string))
        # Native Markdown previews contain no HTML IMG nodes. Their discovered
        # image URLs still participate in rewriting and completion checks.
        for url in plan["urls"]:
            local_url(url)
        queue = list(used)
        while queue:
            url = queue.pop()
            for dependency in fetcher.entries.get(url, {}).get("dependencies", []):
                if dependency not in used and fetcher.entries.get(dependency, {}).get("status") == "downloaded":
                    used.add(dependency)
                    queue.append(dependency)
        old_export = rec.get("export") or {}
        ignored_urls = {x["url"] for x in plan["ignored"]} - set(plan["urls"])
        for url in old_export.get("external_resources", []):
            canonical = _canonical(url)
            if canonical not in ignored_urls and canonical not in used:
                failed.add(canonical)
        new_html = str(plan["soup"])
        html_path = plan["html_path"]
        if html_path.read_text() != new_html:
            atomic_bytes(html_path, new_html.encode())
            updated += 1
        md_path = dest / "note.md"
        if md_path.is_file():
            original_md = md_path.read_text()

            def rewrite_markdown(m):
                value = m[2]
                canonical = _canonical(value) if _remote(value) else None
                if canonical not in used:
                    return m[0]
                return m[1] + "(" + local_url(canonical) + (m[3] or "") + ")"

            # Images and inline resource links only; preserve frontmatter and
            # fenced code examples rather than replacing arbitrary URL text.
            lines, fence, frontmatter = [], None, original_md.startswith("---\n")
            for index, line in enumerate(original_md.splitlines(keepends=True)):
                if frontmatter:
                    lines.append(line)
                    if index and line.strip() == "---":
                        frontmatter = False
                    continue
                marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
                if marker:
                    value = marker[1]
                    fence = None if fence and value[0] == fence[0] and len(value) >= len(fence) else value
                    lines.append(line)
                    continue
                if fence or line.startswith(("    ", "\t")):
                    lines.append(line)
                    continue
                chunks = re.split(r"(`+.*?`+)", line)
                lines.append("".join(chunk if i % 2 else re.sub(r'(!?\[[^\]\n]*\])\(([^\s()]+)(\s+"[^"\n]*")?\)', rewrite_markdown, chunk)
                                     for i, chunk in enumerate(chunks)))
            markdown = "".join(lines)
            if markdown != original_md:
                atomic_bytes(md_path, markdown.encode())
                updated += 1
        exported = dict(old_export)
        exported["external_resources"] = sorted(failed)
        exported["warnings"] = [w for w in exported.get("warnings", [])
                                if "external resources were not fetched" not in w and not w.startswith("External mirror incomplete")]
        if failed:
            exported["warnings"].append(f"External mirror incomplete: {len(failed)} resources remain unavailable")
        previous_ignored = old_export.get("external_ignored", [])
        exported["external_ignored"] = [dict(url=url, reason=reason) for url, reason in sorted(
            {(item["url"], item["reason"]) for item in [*previous_ignored, *plan["ignored"]]})]
        rec["export"] = exported
        # Include all successful transitive dependencies in this record's hash
        # inventory, so modifying a shared cached image cannot go unnoticed.
        paths = {entry["path"] for entry in rec["files"]}
        for url in used:
            entry = fetcher.entries[url]
            paths.add(entry["path"])
            if entry.get("source_path"):
                paths.add(entry["source_path"])
        rec["files"] = [{"path": rel, "size": (p := _contained(root, rel)).stat().st_size, "sha256": _digest(p)}
                        for rel in sorted(paths)]
        write_json(plan["record_path"], rec)
    manifest = {"resources": {**prior, **fetcher.entries}, "schema_errors": schema_errors,
                "ignored": [x for p in plans for x in p["record"]["export"].get("external_ignored", [])],
                "skipped_records": skipped, "limits": {"resource_bytes": MAX_RESOURCE_BYTES, "total_bytes": MAX_TOTAL_BYTES,
                "resources": MAX_RESOURCES, "css_depth": MAX_CSS_DEPTH, "css_references_per_file": MAX_CSS_REFERENCES}}
    _write_control(manifest_path, manifest)
    statuses = [entry.get("status") for entry in fetcher.entries.values()]
    result = {"documents_scanned": len(plans), "resources_mirrored": statuses.count("downloaded"),
            "resources_failed": statuses.count("failed"), "ignored_references": sum(len(p["ignored"]) for p in plans),
            "skipped_invalid_records": len(skipped), "reading_files_updated": updated,
            "manifest_schema_errors": len(schema_errors), "network_bytes": fetcher.bytes_received, "manifest": str(manifest_path)}
    _write_control(progress_path, {"phase": "partial" if result["resources_failed"] or skipped or schema_errors else "complete", **result})
    return result
