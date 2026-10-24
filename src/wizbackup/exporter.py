"""Lossless extraction and offline reading copies of WizNote ZIW documents.

The caller owns the downloaded original. This module never changes that file.
Generated Markdown is a reading/export copy; the original HTML, resources and
metadata remain available in an extraction directory addressed by SHA-256.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata
from urllib.parse import quote, unquote, urlsplit, urlunsplit
import zipfile

from bs4 import BeautifulSoup

MAX_FILES = 20_000
MAX_UNCOMPRESSED = 2 * 1024**3
MAX_MEMBER_SIZE = 1024**3
MAX_BODY_SIZE = 64 * 1024**2
MAX_COMPRESSION_RATIO = 10_000
_SECRET_KEY = re.compile(r"token|cookie|password|passwd|authorization|credential|secret|private.?key", re.I)
_CSP = ("default-src 'none'; img-src 'self' data:; media-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
        "base-uri 'none'; form-action 'none'; frame-src 'none'; connect-src 'none'")
_OLD_CLIENT_ICON_FONTS = "/Applications/WizNote.app/Contents/Resources/files/wizeditor/dependency/fonts.css"


class UnsafeArchive(ValueError):
    """An archive cannot safely or completely be extracted."""


def _truncate_utf8(value: str, length: int) -> str:
    return value.encode("utf-8")[:max(0, length)].decode("utf-8", errors="ignore")


def safe_filename(value: str, *, identity: str | None = None, max_bytes: int = 180) -> str:
    """Portable Unicode component; altered names get a stable collision suffix.

    Supply the document GUID as identity when equal titles need distinct paths.
    The length limit applies to encoded bytes, including any suffix/extension.
    """
    if max_bytes < 32:
        raise ValueError("max_bytes must be at least 32")
    original = str(value)
    normalized = unicodedata.normalize("NFC", original)
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", normalized).strip(" .")
    if not cleaned:
        cleaned = "untitled"
    if cleaned.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        cleaned = "_" + cleaned
    changed = cleaned != original or len(cleaned.encode("utf-8")) > max_bytes
    suffix = "--" + hashlib.sha256((identity or original).encode("utf-8")).hexdigest()[:10] if identity or changed else ""
    extension = PurePosixPath(cleaned).suffix
    if len(extension.encode("utf-8")) > 20:
        extension = ""
    stem = cleaned[:-len(extension)] if extension else cleaned
    stem = _truncate_utf8(stem, max_bytes - len(suffix.encode()) - len(extension.encode("utf-8"))).rstrip(" .") or "untitled"
    return stem + suffix + extension


def _clean_metadata(value):
    if isinstance(value, dict):
        return {str(k): _clean_metadata(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [_clean_metadata(v) for v in value]
    if isinstance(value, str):
        # Signed/authenticated URLs are not useful durable metadata. Remove
        # their credential-bearing query parameters without contacting them.
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value):
            try:
                parts = urlsplit(value)
                port = parts.port
            except ValueError:
                return "[invalid URL omitted]"
            query = "&".join(p for p in parts.query.split("&") if not _SECRET_KEY.search(unquote(p.split("=", 1)[0])))
            hostname = parts.hostname or ""
            if port:
                hostname += f":{port}"
            return urlunsplit((parts.scheme, hostname, parts.path, query, parts.fragment))
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    mode = "wb" if isinstance(content, bytes) else "w"
    with path.open(mode, **({} if isinstance(content, bytes) else {"encoding": "utf-8"})) as handle:
        os.chmod(path, 0o600)
        handle.write(content)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _identical_members(archive: zipfile.ZipFile, first: zipfile.ZipInfo, second: zipfile.ZipInfo) -> bool:
    """Check actual member bytes; matching ZIP CRC/size alone is insufficient."""
    if first.file_size != second.file_size or first.CRC != second.CRC or first.is_dir() != second.is_dir():
        return False
    with archive.open(first) as left, archive.open(second) as right:
        while True:
            left_block, right_block = left.read(1024 * 1024), right.read(1024 * 1024)
            if left_block != right_block:
                return False
            if not left_block:
                return True


def _archive_plan(archive: zipfile.ZipFile) -> tuple[list, dict[str, str], int]:
    entries = archive.infolist()
    if len(entries) > MAX_FILES:
        raise UnsafeArchive("Archive has too many entries")
    if sum(info.file_size for info in entries) > MAX_UNCOMPRESSED:
        raise UnsafeArchive("Archive exceeds the uncompressed size limit")
    mapping: dict[str, str] = {}
    used: dict[str, str] = {}
    seen_members: dict[str, zipfile.ZipInfo] = {}
    duplicate_entries = 0
    files: set[str] = set()
    directories: set[str] = set()
    plan = []
    for info in entries:
        name = info.filename
        parts = PurePosixPath(name).parts
        if (not name or name.startswith(("/", "\\")) or "\\" in name
                or re.match(r"^[a-zA-Z]:", name) or any(p in {"", "..", "."} for p in name.rstrip("/").split("/"))
                or any(ord(ch) < 32 for ch in name) or not parts):
            raise UnsafeArchive("Archive contains an unsafe path")
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
            raise UnsafeArchive("Archive contains a link or special file")
        if info.flag_bits & 1:
            raise UnsafeArchive("ZIP member is password encrypted")
        if info.file_size > MAX_MEMBER_SIZE:
            raise UnsafeArchive("Archive member exceeds the size limit")
        if info.file_size > 1024**2 and info.file_size / max(1, info.compress_size) > MAX_COMPRESSION_RATIO:
            raise UnsafeArchive("Archive compression ratio exceeds the safety limit")
        source = name.rstrip("/")
        if source in seen_members:
            if not _identical_members(archive, seen_members[source], info):
                raise UnsafeArchive("Archive contains duplicate entries with conflicting content or types")
            # Wiz's own encrypted-note packages can repeat an identical CSS
            # member. Keep the original ZIP unchanged, extract the first copy
            # once and record the skipped duplicate count for local auditing.
            duplicate_entries += 1
            continue
        seen_members[source] = info
        # Map every component consistently, including directory case/Unicode
        # collisions on the default macOS filesystem.
        target_parts = []
        for index, component in enumerate(parts):
            source_prefix = "/".join(parts[:index + 1])
            if source_prefix in mapping:
                target_parts = mapping[source_prefix].split("/")
                continue
            candidate = "/".join(target_parts + [safe_filename(component)])
            key = unicodedata.normalize("NFC", candidate).casefold()
            if key in used and used[key] != source_prefix:
                candidate = "/".join(target_parts + [safe_filename(component, identity=source_prefix)])
                key = unicodedata.normalize("NFC", candidate).casefold()
            used[key] = source_prefix
            mapping[source_prefix] = candidate
            target_parts = candidate.split("/")
        target = "/".join(target_parts)
        if info.is_dir():
            if target in files:
                raise UnsafeArchive("Archive file/directory paths conflict")
            directories.add(target)
        else:
            if target in directories:
                raise UnsafeArchive("Archive file/directory paths conflict")
            files.add(target)
        directories.update(str(p) for p in PurePosixPath(target).parents if str(p) != ".")
        if files & directories:
            raise UnsafeArchive("Archive file/directory paths conflict")
        plan.append((info, target))
    return plan, mapping, duplicate_entries


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeError:
            pass
    return data.decode("utf-8", errors="replace")


def _frontmatter(metadata: dict) -> str:
    fields = {
        "title": metadata.get("title", "Untitled"),
        "wiz_guid": metadata.get("guid", metadata.get("documentGuid", metadata.get("document_guid", ""))),
        "category": metadata.get("category", metadata.get("location", "")),
        "created": metadata.get("created", metadata.get("dtCreated", metadata.get("dt_created"))),
        "modified": metadata.get("modified", metadata.get("dtModified", metadata.get("dt_modified"))),
        "tags": metadata.get("tags", []),
        "wiz_metadata": metadata,
    }
    return "---\n" + "\n".join(f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items()) + "\n---\n\n"


def _markdown_payload(soup: BeautifulSoup, metadata: dict | None = None) -> str | None:
    for selector in ("pre#wiz-code", "pre#wiz-markdown", "pre.wiz-markdown", 'script[type="text/markdown"]'):
        node = soup.select_one(selector)
        if node is not None:
            return node.get_text() if node.name != "script" else (node.string or "")
    # Current REST downloads wrap native Markdown in a plain, text-only PRE;
    # there is no special id/class. Require both a metadata hint and that exact
    # wrapper structure so an HTML note merely named *.md is still converted
    # from its actual HTML instead of mistaken for Markdown source.
    metadata = metadata or {}
    hint = str(metadata.get("title", "")).rstrip().lower().endswith((".md", ".markdown"))
    for key in ("type", "fileType", "file_type", "documentType"):
        value = str(metadata.get(key, "")).lower().split(";", 1)[0].strip()
        hint = hint or value in {"markdown", "text/markdown", "text/x-markdown", "lite/markdown"}
    if hint:
        body = soup.body or soup
        children = [node for node in body.children if getattr(node, "name", None) or str(node).strip()]
        if len(children) == 1 and getattr(children[0], "name", None) == "pre" and children[0].find(True) is None:
            return children[0].get_text()
    return None


def _resolve_reference(value: str, body_name: str, mapping: dict[str, str], prefix: str,
                       missing: set[str], external: set[str], *, resource: bool = False) -> str:
    value = value.strip()
    if not value or value.startswith("#"):
        return value
    parts = urlsplit(value)
    if parts.scheme in {"http", "https"} or value.startswith("//"):
        if resource:
            external.add(value)
        return value
    if parts.scheme:
        if parts.scheme.lower() in {"javascript", "vbscript", "file"}:
            return "#blocked-unsafe-reference"
        return value
    raw_path = unquote(parts.path).replace("\\", "/")
    # Resolve relative links without ever accessing paths outside the archive.
    normalized = []
    for part in (PurePosixPath(body_name).parent / raw_path.lstrip("/")).parts:
        if part in {".", ""}:
            continue
        if part == "..":
            if not normalized:
                missing.add(raw_path)
                return value
            normalized.pop()
        else:
            normalized.append(part)
    source = "/".join(normalized)
    target = mapping.get(source)
    if target is None:
        if resource:
            missing.add(raw_path)
        return value
    return urlunsplit(("", "", quote(prefix + "/" + target, safe="/"), parts.query, parts.fragment))


def _uses_client_icon_font(soup: BeautifulSoup) -> bool:
    body = soup.body or soup
    for node in [body, *body.find_all(True)]:
        if any(str(value).startswith("editor-icon") for value in node.get("class", [])):
            return True
        if "wizeditoricons" in str(node.get("font-family", node.get("face", ""))).lower():
            return True
        if "wizeditoricons" in str(node.get("style", "")).lower():
            return True
    # A custom rule can apply the icon font without an editor-icon class.
    # Keep the dependency whenever inline CSS mentions this font family;
    # guessing which selectors apply would risk dropping visible content.
    return any("wizeditoricons" in style.get_text().lower() for style in soup.find_all("style"))


def _ignore_unused_client_dependencies(soup: BeautifulSoup, ignored: list) -> None:
    if _uses_client_icon_font(soup):
        return
    for link in list(soup.find_all("link")):
        href = link.get("href")
        if not isinstance(href, str) or "stylesheet" not in link.get("rel", []):
            continue
        try:
            parts = urlsplit(href.strip())
        except ValueError:
            continue
        if (parts.scheme not in {"", "file"} or parts.netloc or parts.query or parts.fragment
                or unquote(parts.path) != _OLD_CLIENT_ICON_FONTS):
            continue
        ignored.append({"href": href, "reason": "unused-client-editor-icon-fonts"})
        link.decompose()


def _offline_html(body: str, body_name: str, mapping: dict[str, str], prefix: str,
                  *, ignored_client_dependencies: list | None = None):
    soup = BeautifulSoup(body, "html.parser")
    missing: set[str] = set()
    external: set[str] = set()
    for node in soup.find_all(["script", "base", "iframe", "object", "embed", "applet", "form"]):
        node.decompose()
    _ignore_unused_client_dependencies(soup, ignored_client_dependencies if ignored_client_dependencies is not None else [])
    for meta in soup.find_all("meta"):
        if str(meta.get("http-equiv", "")).lower() in {"refresh", "content-security-policy"}:
            meta.decompose()
    for node in soup.find_all(True):
        for attr in list(node.attrs):
            if attr.lower().startswith("on"):
                del node.attrs[attr]
        for attr in ("src", "poster", "href", "background", "xlink:href"):
            if isinstance(node.get(attr), str):
                resource = attr != "href" or node.name == "link"
                node[attr] = _resolve_reference(node[attr], body_name, mapping, prefix, missing, external, resource=resource)
        if node.get("srcset"):
            # src takes precedence in the offline view. Keep srcset unchanged
            # in the original, avoiding ambiguous parsing of data URLs here.
            del node["srcset"]
        if node.name == "a":
            node["rel"] = "noreferrer noopener"
        if node.get("style"):
            node["style"] = _rewrite_css(node["style"], body_name, mapping, prefix, missing, external)
    for style in soup.find_all("style"):
        if style.string:
            style.string.replace_with(_rewrite_css(style.string, body_name, mapping, prefix, missing, external))
    if soup.html is None:
        wrapper = BeautifulSoup("<!doctype html><html><head><meta charset='utf-8'></head><body></body></html>", "html.parser")
        for child in list(soup.contents):
            wrapper.body.append(child.extract())
        soup = wrapper
    elif soup.head is None:
        soup.html.insert(0, soup.new_tag("head"))
    csp = soup.new_tag("meta")
    csp["http-equiv"] = "Content-Security-Policy"
    csp["content"] = _CSP
    soup.head.insert(0, csp)
    referrer = soup.new_tag("meta", attrs={"name": "referrer", "content": "no-referrer"})
    soup.head.insert(1, referrer)
    return str(soup), sorted(missing), sorted(external)


def _rewrite_css(css: str, body_name: str, mapping: dict[str, str], prefix: str, missing: set[str], external: set[str]) -> str:
    def replace(match):
        value = match.group(2).strip()
        rewritten = _resolve_reference(value, body_name, mapping, prefix, missing, external, resource=True)
        return f'url("{rewritten.replace(chr(34), "%22")}")'
    return re.sub(r"url\(\s*(['\"]?)(.*?)\1\s*\)", replace, css, flags=re.I)


def _rewrite_markdown(body: str, body_name: str, mapping: dict[str, str], prefix: str,
                      missing: set[str], external: set[str]) -> str:
    # Leave prose/code intact; rewrite ordinary inline links and images only.
    def replace(match):
        value = match.group(2)
        rewritten = _resolve_reference(value, body_name, mapping, prefix, missing, external,
                                       resource=match.group(1).startswith("!"))
        return match.group(1) + "(" + rewritten + match.group(3) + ")"
    pattern = r'(!?\[[^\]\n]*\])\(([^\s()]+)(\s+"[^"\n]*")?\)'
    output = []
    fence = None
    for line in body.splitlines(keepends=True):
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
        # Inline code spans are examples, not image/link references.
        chunks = re.split(r"(`+.*?`+)", line)
        output.append("".join(chunk if index % 2 else re.sub(pattern, lambda m: replace(_OptionalGroupMatch(m)), chunk)
                              for index, chunk in enumerate(chunks)))
    return "".join(output)


class _OptionalGroupMatch:
    """Make optional regex group concatenation explicit and predictable."""
    def __init__(self, match):
        self.match = match

    def group(self, number):
        return self.match.group(number) or ""


def _html_markdown(offline: str) -> tuple[str, list[str]]:
    """Convert a sanitized reading copy without discarding explicit targets.

    Markdownify normally drops HTML id/name attributes, including empty named
    anchors. Emit only inert, escaped anchors; never carry source attributes
    into generated HTML. This deliberately does not infer heading slugs or
    attempt to compute visibility from external CSS.
    """
    from markdownify import MarkdownConverter

    soup = BeautifulSoup(offline, "html.parser")
    excluded = {"head", "style", "title", "script", "template", "noscript",
                "pre", "code", "kbd", "samp", "iframe", "object", "embed",
                "applet", "form"}
    hidden_style = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\s*(?:!important\s*)?(?:;|$)", re.I)
    anchors: dict[int, list[str]] = {}
    preserved: list[str] = []
    seen: set[str] = set()
    for node in soup.find_all(True):
        if node.name == "html":
            continue
        ancestry = [node, *node.parents]
        if any(parent.name in excluded or parent.has_attr("hidden")
               or (parent.name == "input" and str(parent.get("type", "")).lower() == "hidden")
               or hidden_style.search(re.sub(r"/\*.*?\*/", "", str(parent.get("style", "")), flags=re.S))
               for parent in ancestry):
            continue
        target = node
        # An <a> in another link's label would create invalid nested links.
        # Put its targets immediately before the containing link instead.
        link_parent = node.find_parent("a", href=True)
        if link_parent is not None:
            target = link_parent
        # Structural table elements have no standalone Markdown equivalent.
        # Keep their targets at the start of that same visible table.
        if target.name in {"thead", "tbody", "tfoot", "tr", "colgroup", "col", "caption"}:
            target = target.find_parent("table") or target
        values = [node.get("id")]
        if node.name == "a":
            values.append(node.get("name"))
        for value in values:
            if (not isinstance(value, str) or not value
                    or re.search(r"[\x00-\x20\x7f]", value) or value in seen):
                continue
            # Reserve in document order, before conversion recurses into any
            # child. Duplicates retain the original first target, without a
            # fabricated suffix or a silently overwritten destination.
            seen.add(value)
            preserved.append(value)
            anchors.setdefault(id(target), []).append(value)

    class AnchorConverter(MarkdownConverter):
        def get_conv_fn(self, tag_name):
            convert = super().get_conv_fn(tag_name)

            def anchored(node, text, parent_tags):
                ids = anchors.get(id(node), ())
                # Pipes must not split Markdown cells, and URI-like ids must
                # not be mistaken for bare note links during catalog rewrite.
                # Entities preserve the actual HTML attribute value.
                prefix = "".join('<a id="' + html.escape(value, quote=True).replace("|", "&#124;").replace(":", "&#58;") + '"></a>'
                                 for value in ids)
                if node.name in {"a", "img", "hr", "br", "table"}:
                    return prefix + (convert(node, text, parent_tags) if convert else text)
                # Add before tag formatting so headings, list bullets, quotes
                # and table cells retain their Markdown structure. Returned
                # HTML bypasses text escaping and cannot be swallowed by the
                # ordinary empty-<a> converter.
                return convert(node, prefix + text, parent_tags) if convert else prefix + text

            return anchored

    markdown = AnchorConverter(heading_style="ATX", strip=["style", "meta", "title", "head"]).convert_soup(soup)
    return markdown, preserved


def export_note(raw_path: Path, dest: Path, metadata: dict) -> dict:
    """Create offline reading copies; return an honest, machine-readable result.

    status is exported, locked or unsupported. Warnings/missing_resources and
    external_resources must be considered by callers before claiming a fully
    self-contained readable export. A raw archive remains the recovery source.
    """
    raw_path, dest = Path(raw_path), Path(dest)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    clean = _clean_metadata(metadata)
    result = {"status": "unsupported", "html_file": None, "md_file": None,
              "resources": [], "count": 0, "resource_count": 0, "warnings": [],
              "missing_resources": [], "external_resources": [],
              "ignored_client_dependencies": [],
              "preserved_anchor_ids": [],
              "duplicate_entries": 0,
              "raw_sha256": _sha256(raw_path)}

    def finish():
        _write(dest / "metadata.json", json.dumps({"metadata": clean, "export": result}, ensure_ascii=False, indent=2) + "\n")
        return result

    if not zipfile.is_zipfile(raw_path):
        with raw_path.open("rb") as handle:
            magic = handle.read(16)
        encrypted = clean.get("encrypted", clean.get("isEncrypted", clean.get("protected", False)))
        is_locked = str(encrypted).lower() in {"true", "1", "yes"} or magic.startswith((b"ZIWR", b"WIZR", b"WIZENC"))
        result["status"] = "locked" if is_locked else "unsupported"
        result["warnings"].append("Encrypted original retained; decryption requires the user's certificate/password" if is_locked else "Original is not a recognized ZIW ZIP archive; retained without conversion")
        return finish()
    staging = None
    try:
        with zipfile.ZipFile(raw_path) as archive:
            plan, mapping, duplicate_entries = _archive_plan(archive)
            result["duplicate_entries"] = duplicate_entries
            extraction = dest / "original" / result["raw_sha256"][:16]
            if not extraction.exists():
                extraction.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                staging = Path(tempfile.mkdtemp(prefix=".extract-", dir=dest))
                for info, name in plan:
                    target = staging / name
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with archive.open(info) as source, target.open("wb") as output:
                            os.chmod(target, 0o600)
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                os.replace(staging, extraction)
                staging = None
            files = [(info, name) for info, name in plan if not info.is_dir()]
            md_files = [(i, n) for i, n in files if n.lower().endswith((".md", ".markdown"))]
            html_files = [(i, n) for i, n in files if n.lower().endswith((".html", ".htm"))]
            priority = lambda item: (len(PurePosixPath(item[0].filename).parts), PurePosixPath(item[0].filename).name.lower() not in {"index.html", "index.htm", "index.md", "index.markdown"}, item[0].filename)
            candidates = md_files + html_files
            if not candidates:
                result["warnings"].append("Archive extracted intact but no supported HTML/Markdown body was found")
                return finish()
            info, mapped_body = sorted(candidates, key=priority)[0]
            if info.file_size > MAX_BODY_SIZE:
                result["warnings"].append("Archive extracted intact; body exceeds the safe in-memory conversion limit")
                return finish()
            body = _decode((extraction / mapped_body).read_bytes())
            prefix = extraction.relative_to(dest).as_posix()
            result["extraction_dir"] = prefix
            result["body_original"] = prefix + "/" + mapped_body
            result["member_paths"] = {name: prefix + "/" + mapped for name, mapped in mapping.items()}
            result["resources"] = [prefix + "/" + name for i, name in files if i.filename != info.filename]
            result["count"] = result["resource_count"] = len(result["resources"])
            if info.filename.lower().endswith((".md", ".markdown")):
                markdown = body
            else:
                markdown = _markdown_payload(BeautifulSoup(body, "html.parser"), clean)
            if markdown is not None:
                missing: set[str] = set()
                external: set[str] = set()
                markdown = _rewrite_markdown(markdown, info.filename, mapping, prefix, missing, external)
                result["missing_resources"] = sorted(missing)
                result["external_resources"] = sorted(external)
                preview = "<!doctype html><html><head><meta charset='utf-8'></head><body><pre style='white-space:pre-wrap'>" + html.escape(markdown) + "</pre></body></html>"
                offline, _, _ = _offline_html(preview, info.filename, mapping, prefix,
                                              ignored_client_dependencies=result["ignored_client_dependencies"])
            else:
                offline, missing, external = _offline_html(body, info.filename, mapping, prefix,
                                                          ignored_client_dependencies=result["ignored_client_dependencies"])
                result["missing_resources"] = missing
                result["external_resources"] = external
                try:
                    markdown, result["preserved_anchor_ids"] = _html_markdown(offline)
                except ImportError:
                    result["warnings"].append("Install markdownify to generate a Markdown reading copy")
                    _write(dest / "index.html", offline)
                    result["html_file"] = "index.html"
                    return finish()
            _write(dest / "index.html", offline)
            _write(dest / "note.md", _frontmatter(clean) + markdown)
            result.update(status="exported", html_file="index.html", md_file="note.md")
            if result["missing_resources"]:
                result["warnings"].append(f"{len(result['missing_resources'])} referenced local resources are absent from the original archive")
            if result["external_resources"]:
                result["warnings"].append(f"{len(result['external_resources'])} external resources were not fetched and are blocked in the offline HTML")
    except (UnsafeArchive, zipfile.BadZipFile, RuntimeError, OSError, ValueError) as error:
        result["warnings"].append(f"Archive conversion refused: {type(error).__name__}: {error}")
    finally:
        if staging is not None:
            # Only our own unpublished staging output, never any user original.
            shutil.rmtree(staging)
    return finish()
