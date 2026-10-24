"""Archive collaboration documents using read-only editor REST endpoints.

Only the token issuance endpoint uses POST. Every document operation is GET;
no WebSocket, editing operation, subscription, import, or save is performed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote, unquote, urljoin, urlsplit
import zipfile

from bs4 import BeautifulSoup
import requests

from .client import ApiError, AuthError, atomic_bytes, safe_url, write_json
from .exporter import export_note, safe_filename


def _json(response):
    try:
        value = response.json()
    except ValueError:
        raise ApiError("Editor returned invalid JSON") from None
    if isinstance(value, dict):
        code = value.get("returnCode")
        if code not in (None, 200):
            if code in (301, 401, 403):
                raise AuthError("Editor authentication expired")
            raise ApiError("Editor API rejected the request")
        if value.get("error") or value.get("statusCode", 200) >= 400:
            raise ApiError("Editor API returned an error")
    return value


def _request(client, method, url, *, headers, params=None, stream=False):
    """Keep sensitive headers on the exact KB host, including on redirects."""
    origin = urlsplit(client.server).netloc
    safe_url(url)
    if urlsplit(url).netloc != origin:
        raise ApiError("Editor endpoint must use the configured KB origin")
    for attempt in range(min(client.retries, 3) + 1):
        try:
            current = url
            for redirect in range(6):
                safe_url(current)
                same_origin = urlsplit(current).netloc == origin
                response = client.session.request(
                    method, current, headers=headers if same_origin else {},
                    params=params if redirect == 0 else None,
                    timeout=(15, client.timeout), stream=stream, allow_redirects=False,
                )
                if response.is_redirect:
                    location = response.headers.get("Location", "")
                    response.close()
                    if method != "GET":
                        raise ApiError("Refusing redirect of editor token issuance")
                    current = safe_url(urljoin(current, location))
                    continue
                break
            else:
                raise ApiError("Too many editor resource redirects")
            if response.status_code in (401, 403):
                response.close()
                raise AuthError("Editor authentication expired or access denied")
            if response.status_code == 429 or response.status_code >= 500:
                response.close()
                if attempt < min(client.retries, 3):
                    time.sleep(min(2 ** attempt, 4))
                    continue
                raise ApiError("Editor temporarily unavailable")
            if response.status_code >= 400:
                status = response.status_code
                response.close()
                raise ApiError(f"Editor HTTP {status}")
            return response
        except requests.RequestException:
            if attempt >= min(client.retries, 3):
                raise ApiError("Editor network request failed (credentials redacted)") from None
            time.sleep(min(2 ** attempt, 4))
    raise ApiError("Editor request retry budget exhausted")


def _resource_name(value):
    if not isinstance(value, str) or not value or value in (".", ".."):
        raise ApiError("Invalid editor resource name")
    if any(ch in value for ch in ("/", "\\", "\x00")) or any(ord(ch) < 32 for ch in value):
        raise ApiError("Unsafe editor resource name")
    return value


def _localize_html(body: str, mapping: dict[str, str], base: str):
    soup = BeautifulSoup(body, "html.parser")
    base_parts = urlsplit(base)
    missing = set()
    malformed_links = 0
    for tag in soup.find_all(True):
        for attr in ("src", "href", "poster", "data-src"):
            value = tag.get(attr)
            if not isinstance(value, str) or value.startswith(("#", "data:")):
                continue
            try:
                parts = urlsplit(value)
            except ValueError:
                # Old documents can contain fullwidth delimiters or broken
                # IPv6 authorities. Preserve the server export verbatim, but
                # block an unusable URL in the derived archive so one malformed
                # navigation link cannot prevent exporting the whole document.
                tag[attr] = "#invalid-reference"
                if attr == "href":
                    malformed_links += 1
                if attr != "href" or tag.name == "link":
                    missing.add("malformed-" + attr + "-" + hashlib.sha256(value.encode()).hexdigest()[:16])
                continue
            if parts.scheme or parts.netloc:
                if parts.scheme != "https" or parts.netloc != base_parts.netloc:
                    continue
                if not parts.path.startswith(base_parts.path + "/resources/"):
                    continue
            path = unquote(parts.path)
            key = path.rsplit("/", 1)[-1]
            if key in mapping:
                tag[attr] = "index_files/" + quote(mapping[key], safe="")
            elif attr in ("src", "poster", "data-src") and not parts.scheme:
                missing.add(key)
    return str(soup), sorted(missing), malformed_links


def _download(client, url, target: Path, headers):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".resource-", dir=target.parent)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(fd, "wb") as output:
            with _request(client, "GET", url, headers=headers, stream=True) as response:
                # JSON is itself a valid attachment/resource format. The editor
                # resource endpoint reports errors through its HTTP status;
                # do not reinterpret user file fields as API responses.
                expected_length = response.headers.get("Content-Length")
                encoded = response.headers.get("Content-Encoding")
                for chunk in response.iter_content(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                if expected_length and not encoded and size != int(expected_length):
                    raise ApiError("Editor resource size mismatch")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except requests.RequestException:
        raise ApiError("Editor resource download interrupted (credentials redacted)") from None
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"size": size, "sha256": digest.hexdigest()}


def archive_collaboration(client, note: dict, dest: Path) -> dict:
    """Save current JSON snapshot, official HTML, all resources and a ZIW copy.

    ``dest`` is a local per-note archival directory. Returned paths are absolute.
    Token responses are never saved. Resource failures are explicit and produce
    ``status=partial``; original JSON and HTML remain available for later repair.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    kb, doc = str(client.kb), str(note["docGuid"])
    if not re.fullmatch(r"[A-Za-z0-9-]+", kb) or not re.fullmatch(r"[A-Za-z0-9-]+", doc):
        raise ApiError("Invalid collaboration document identifier")
    base = f"{client.server}/editor/{kb}/{doc}"
    token_url = f"{client.server}/ks/note/{kb}/{doc}/tokens"
    with _request(client, "POST", token_url, headers={"x-wiz-token": client.token}) as response:
        issued = _json(response)
    try:
        token = issued["result"]["editorToken"]
        if not isinstance(token, str) or not token:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ApiError("Editor token issuance returned no token") from None
    headers = {
        "x-live-editor-token": token,
        "x-live-editor-base-url": base64.b64encode(base.encode()).decode("ascii"),
    }
    with _request(client, "GET", base + "/versions/latest", headers=headers,
                  params={"metaOnly": "false", "cookie": "false"}) as response:
        latest = _json(response)
    try:
        snapshot = latest["data"]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("blocks"), list):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ApiError("Editor returned no usable document snapshot") from None
    write_json(dest / "latest.json", latest)
    write_json(dest / "snapshot.json", snapshot)
    with _request(client, "GET", base + "/export/html", headers=headers,
                  params={"os": "false"}) as response:
        html_bytes = response.content
    body = html_bytes.decode("utf-8-sig")
    if "<html" not in body.lower() and "<body" not in body.lower():
        raise ApiError("Editor HTML export returned no HTML document")
    atomic_bytes(dest / "server-export.html", html_bytes)
    with _request(client, "GET", base + "/resources", headers=headers) as response:
        resource_names = _json(response)
    if not isinstance(resource_names, list):
        raise ApiError("Editor returned an invalid resource inventory")
    resource_names = list(dict.fromkeys(_resource_name(name) for name in resource_names))
    write_json(dest / "resources.json", resource_names)
    resources = []
    failures = []
    mapping = {}
    for name in resource_names:
        local_name = safe_filename(name, identity=name)
        target = dest / "index_files" / local_name
        try:
            record = _download(client, base + "/resources/" + quote(name, safe=""), target, headers)
            record.update({"name": name, "local_name": local_name})
            resources.append(record)
            mapping[name] = local_name
        except AuthError:
            raise
        except (ApiError, OSError):
            failures.append({"name": name, "error": "Resource download failed; retry required"})
    local_body, missing, malformed_links = _localize_html(body, mapping, base)
    atomic_bytes(dest / "index.html", local_body.encode("utf-8"))
    manifest = {"resources": resources, "failed_resources": failures,
                "missing_html_resources": missing, "malformed_links": malformed_links,
                "kind": "reconstructed-collaboration-archive"}
    write_json(dest / "resource-manifest.json", manifest)
    archive = dest / "content.ziw"
    temporary = archive.with_name(archive.name + ".part")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as package:
        package.write(dest / "index.html", "index.html")
        package.write(dest / "snapshot.json", "collaboration-snapshot.json")
        package.write(dest / "resource-manifest.json", "backup-manifest.json")
        for item in resources:
            package.write(dest / "index_files" / item["local_name"], "index_files/" + item["local_name"])
    os.chmod(temporary, 0o600)
    temporary.replace(archive)
    exported = export_note(archive, dest / "readable", note)
    status = "partial" if failures or missing or exported.get("status") != "exported" else "archived"
    result = {"status": status, "archive_path": str(archive.resolve()),
              "snapshot_path": str((dest / "snapshot.json").resolve()),
              "resources": len(resources), "resource_count": len(resource_names),
              "failed_resources": failures, "missing_html_resources": missing,
              "malformed_links": malformed_links, "export": exported, "snapshot_mtime": latest.get("mtime")}
    write_json(dest / "collaboration-report.json", result)
    return result
