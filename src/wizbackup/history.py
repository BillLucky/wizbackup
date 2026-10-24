"""Read-only, resumable archival of the versions exposed by Wiz history/list.

The view URL contains legacy parameters named ``revertClient*``; this module
only GETs history/view. It never calls the separate POST history/revert route.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import quote, unquote, urlsplit
import zipfile

from bs4 import BeautifulSoup
import requests

from .client import ApiError, AuthError, atomic_bytes, safe_url, write_json
from .exporter import export_note, safe_filename


def _sha256(path: Path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _container(payload):
    if not isinstance(payload, dict):
        raise ApiError("Invalid history inventory")
    if payload.get("returnCode", 200) != 200:
        raise ApiError("History inventory contains an API error")
    for candidate in (payload.get("result"), payload.get("history"), payload):
        if isinstance(candidate, dict) and isinstance(candidate.get("versionInfos"), list):
            return candidate
    raise ApiError("History inventory has no versionInfos array")


def _version_key(version):
    try:
        number = int(version["version"])
        if number < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ApiError("Historical version has no valid version number") from None
    identity = json.dumps({k: version.get(k) for k in ("version", "editorGuid", "objType")}, sort_keys=True)
    return str(number) + "--" + hashlib.sha256(identity.encode()).hexdigest()[:12]


def _view(client, doc, version):
    url = safe_url(f"{client.server}/ks/history/view/{client.kb}/{doc}")
    params = {
        "version": str(version["version"]),
        "editorGuid": str(version.get("editorGuid") or ""),
        "revertClientType": str(version.get("clientType") or "Desktop-mac"),
        "revertClientVersion": str(version.get("clientVersion") or "0.1.107"),
    }
    # A bounded retry here avoids spending the main client's full exponential
    # retry budget on every version during a systemic history service outage.
    for attempt in range(min(client.retries, 1) + 1):
        try:
            with client.session.get(
                url, params=params, headers={"x-wiz-token": client.token,
                                            "Origin": "https://www.wiz.cn"},
                timeout=(15, client.timeout), allow_redirects=False,
            ) as response:
                status = response.status_code
                if status in (401, 403):
                    raise AuthError("Historical version access denied or authentication expired")
                if response.is_redirect:
                    raise ApiError("Unexpected history view redirect")
                if status == 429 or status >= 500:
                    if attempt < min(client.retries, 1):
                        time.sleep(1)
                        continue
                    raise ApiError(f"History HTTP {status}")
                if status >= 400:
                    raise ApiError(f"History HTTP {status}")
                try:
                    data = response.json()
                except ValueError:
                    raise ApiError("History view returned invalid JSON") from None
                if not isinstance(data, dict):
                    raise ApiError("History view returned a non-object response")
                code = data.get("returnCode", 200)
                if code in (301, 401, 403):
                    raise AuthError("Historical version authentication expired")
                if code != 200:
                    raise ApiError("History view returned an API error")
                return data
        except requests.RequestException:
            if attempt >= min(client.retries, 1):
                raise ApiError("Historical version network request failed (credentials redacted)") from None
            time.sleep(1)
    raise ApiError("Historical version retry budget exhausted")


def _note_data(payload):
    for candidate in (payload, payload.get("result", {})):
        if isinstance(candidate, dict) and "noteData" in candidate:
            value = candidate["noteData"]
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    raise ApiError("Historical noteData is not JSON") from None
            if isinstance(value, dict):
                return value
    for candidate in (payload.get("result"), payload):
        if isinstance(candidate, dict) and ("html" in candidate or "url" in candidate):
            return candidate
    raise ApiError("History response contains no downloadable noteData")


def _localize(body, mapping):
    soup = BeautifulSoup(body, "html.parser")
    for tag in soup.find_all(True):
        for attr in ("src", "href", "poster", "data-src"):
            source = tag.get(attr)
            if not isinstance(source, str) or source.startswith(("#", "data:")):
                continue
            local = mapping.get(source)
            if local is None:
                parts = urlsplit(source)
                if not parts.scheme and not parts.netloc:
                    path = unquote(parts.path)
                    local = mapping.get(path) or mapping.get(path.removeprefix("index_files/"))
            if local is not None:
                tag[attr] = "index_files/" + quote(local, safe="")
    return str(soup)


def _valid_record(folder, version):
    report = folder / "version-report.json"
    if not report.is_file():
        return None
    try:
        record = json.loads(report.read_text())
        fingerprint = hashlib.sha256(json.dumps(version, sort_keys=True).encode()).hexdigest()
        if record.get("source_fingerprint") != fingerprint or record.get("status") not in ("downloaded", "locked"):
            return None
        for entry in record["files"]:
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                return None
            path = folder / relative
            if not path.is_file() or path.stat().st_size != entry["size"] or _sha256(path) != entry["sha256"]:
                return None
        return record if record["files"] else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _archive_version(client, doc, note, version, folder):
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json(folder / "version.json", version)
    record = {"version": version["version"], "status": "pending", "resources": 0,
              "failed_resources": [], "files": [], "warnings": [],
              "source_fingerprint": hashlib.sha256(json.dumps(version, sort_keys=True).encode()).hexdigest()}
    try:
        response = _view(client, doc, version)
        write_json(folder / "response.json", response)
        data = _note_data(response)
        write_json(folder / "note-data.json", data)
        archive = folder / "original.ziw"
        if data.get("url"):
            client.download(data["url"], archive)
            record["archive_kind"] = "original-server-object"
        elif isinstance(data.get("html"), str):
            body = data["html"]
            atomic_bytes(folder / "original.html", body.encode("utf-8"))
            resources = data.get("resources") or []
            if not isinstance(resources, list):
                raise ApiError("Historical resource list is invalid")
            mapping = {}
            saved = []
            for resource in resources:
                name = resource.get("name") if isinstance(resource, dict) else None
                url = resource.get("url") if isinstance(resource, dict) else None
                if not isinstance(name, str) or not name or not isinstance(url, str):
                    raise ApiError("Historical resource lacks a name or download URL")
                local_name = safe_filename(name, identity=name)
                target = folder / "index_files" / local_name
                try:
                    client.download(url, target, expected_md5=resource.get("md5"))
                    if resource.get("size") is not None and target.stat().st_size != int(resource["size"]):
                        raise ApiError("Historical resource size mismatch")
                    mapping[name] = local_name
                    mapping[url] = local_name
                    saved.append((target, local_name))
                except AuthError:
                    raise
                except (ApiError, OSError, TypeError, ValueError):
                    record["failed_resources"].append(name)
            body = _localize(body, mapping)
            atomic_bytes(folder / "index.html", body.encode("utf-8"))
            archive = folder / "content.ziw"
            part = archive.with_name(archive.name + ".part")
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as package:
                package.write(folder / "index.html", "index.html")
                package.write(folder / "version.json", "backup-history-version.json")
                for path, name in saved:
                    package.write(path, "index_files/" + name)
            part.chmod(0o600)
            part.replace(archive)
            record["resources"] = len(saved)
            record["archive_kind"] = "reconstructed-from-original-html-and-resources"
        else:
            raise ApiError("Historical JSON preserved but no HTML or binary object was supplied")
        exported = export_note(archive, folder / "readable", {**note, **data.get("info", {})})
        record["export"] = exported
        record["status"] = "locked" if exported["status"] == "locked" else "downloaded"
        if (record["failed_resources"] or exported.get("missing_resources")
                or exported["status"] not in ("exported", "locked")):
            record["status"] = "partial"
            record["warnings"].append("Historical version requires resource or format repair")
        record["files"] = [
            {"path": str(path.relative_to(folder)), "size": path.stat().st_size, "sha256": _sha256(path)}
            for path in sorted(folder.rglob("*")) if path.is_file()
            and path.name != "version-report.json" and not path.name.endswith(".part")
        ]
    except AuthError:
        raise
    except (ApiError, OSError, ValueError, zipfile.BadZipFile) as exc:
        record["status"] = "failed"
        record["error"] = str(exc) if isinstance(exc, ApiError) else type(exc).__name__
    write_json(folder / "version-report.json", record)
    return record


def archive_history(client, note: dict, history_payload: dict, dest: Path) -> dict:
    """Archive every listed history version, with explicit unresolved counts.

    Successful versions resume by SHA-256. Three consecutive server failures
    open a per-note circuit breaker; remaining versions are recorded pending,
    not counted as downloaded. A later invocation retries failures and pending
    versions. No cap is imposed on the number of supplied versionInfos.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    doc = str(note["docGuid"])
    if not re.fullmatch(r"[A-Za-z0-9-]+", doc) or not re.fullmatch(r"[A-Za-z0-9-]+", str(client.kb)):
        raise ApiError("Invalid historical document identifier")
    container = _container(history_payload)
    rows = container["versionInfos"]
    write_json(dest / "inventory.json", history_payload)
    warnings = []
    if container.get("hasMore") or container.get("nextPage") or container.get("nextCursor"):
        warnings.append("History list advertises continuation without a documented cursor protocol; inventory incomplete")
    for key in ("total", "totalCount"):
        if container.get(key) is not None and int(container[key]) > len(rows):
            warnings.append("History inventory contains fewer entries than its advertised total")
    records = []
    known = set()
    consecutive_server_errors = 0
    downloaded_so_far = 0
    for version in rows:
        if not isinstance(version, dict):
            raise ApiError("Invalid historical version metadata")
        key = _version_key(version)
        if key in known:
            warnings.append("Duplicate history entry retained in inventory and archived once")
            continue
        known.add(key)
        folder = dest / "versions" / key
        existing = _valid_record(folder, version)
        if existing:
            records.append(existing)
            downloaded_so_far += 1
            consecutive_server_errors = 0
            continue
        if consecutive_server_errors >= 3:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json(folder / "version.json", version)
            record = {"version": version["version"], "status": "pending",
                      "error": "History service circuit breaker open; retry required"}
            write_json(folder / "version-report.json", record)
        else:
            record = _archive_version(client, doc, note, version, folder)
            if record.get("error", "").startswith("History HTTP 5"):
                consecutive_server_errors += 1
            else:
                consecutive_server_errors = 0
        records.append(record)
        downloaded_so_far += record["status"] in ("downloaded", "locked")
        write_json(dest / "progress.json", {"versions": len(rows), "processed": len(records),
                                            "downloaded": downloaded_so_far})
    downloaded = sum(r["status"] in ("downloaded", "locked") for r in records)
    failed = sum(r["status"] in ("failed", "partial") for r in records)
    pending = sum(r["status"] == "pending" for r in records)
    if pending:
        warnings.append("History server errors opened the circuit breaker; remaining versions require a later retry")
    report = {"status": "partial" if failed or pending or warnings else "archived",
              "versions": len(rows), "unique_versions": len(records), "downloaded": downloaded,
              "failed": failed, "pending": pending,
              "locked": sum(r["status"] == "locked" for r in records),
              "warnings": warnings,
              "records": [{k: r[k] for k in ("version", "status", "error") if k in r} for r in records]}
    write_json(dest / "history-report.json", report)
    return report
