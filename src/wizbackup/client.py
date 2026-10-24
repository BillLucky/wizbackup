"""Read-only WizNote client; credentials never leave the configured Wiz hosts."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests


class ApiError(RuntimeError):
    pass


class AuthError(ApiError):
    pass


def normalized_proxy(value):
    """Explicit process-only HTTP(S) proxy; never inherit ambient settings."""
    if value is None or value == "direct":
        return None
    try:
        p = urlsplit(value)
        if (p.scheme not in ("http", "https") or not p.hostname or p.username or p.password
                or p.path not in ("", "/") or p.query or p.fragment or p.port is None):
            raise ValueError
    except (ValueError, TypeError):
        raise ApiError("Proxy must be direct or an HTTP(S) host:port URL without credentials") from None
    return value.rstrip("/")


def is_wiz_host(host):
    return host == "wiz.cn" or bool(host and host.endswith(".wiz.cn"))


def safe_url(url):
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ApiError("Refusing non-HTTPS or credential-bearing URL")
    # Server-issued object URLs may use Wiz's Aliyun bucket. Never send auth there.
    if not (is_wiz_host(p.hostname) or p.hostname.endswith(".aliyuncs.com")):
        raise ApiError("Object host is outside the Wiz/OSS allowlist")
    return url


def atomic_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    with part.open("wb") as out:
        os.chmod(part, 0o600)
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    part.replace(path)


def write_json(path, value):
    atomic_bytes(Path(path), (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode())


class WizClient:
    def __init__(self, auth, network="direct", retries=4, timeout=60, proxy=None):
        self.token = auth["token"]
        self.kb = auth["kb_guid"]
        self.server = auth["kb_server"].rstrip("/")
        safe_url(self.server)
        if not is_wiz_host(urlsplit(self.server).hostname):
            raise ApiError("KB server must be a Wiz HTTPS host")
        self.session = requests.Session()
        self.session.trust_env = False
        selected = proxy if proxy is not None else auth.get("proxy")
        if selected is None and network == "us": selected = "http://127.0.0.1:7897"
        selected = normalized_proxy(selected)
        self.session.proxies = {"http": selected, "https": selected} if selected else {}
        self.retries, self.timeout = retries, timeout

    def request(self, url, *, params=None, stream=False):
        url = safe_url(url)
        for attempt in range(self.retries + 1):
            try:
                current = url
                for redirect in range(6):
                    host = urlsplit(current).hostname
                    headers = {"User-Agent": "wizbackup/0.1", "Accept": "*/*"}
                    if host in {urlsplit(self.server).hostname, "as.wiz.cn"}:
                        headers["x-wiz-token"] = self.token
                        headers["Origin"] = "https://www.wiz.cn"
                    response = self.session.get(current, params=params if redirect == 0 else None,
                                                headers=headers, timeout=(15, self.timeout),
                                                stream=stream, allow_redirects=False)
                    if response.is_redirect:
                        location = response.headers.get("Location", "")
                        response.close()
                        current = safe_url(urljoin(current, location))
                        continue
                    break
                else:
                    raise ApiError("Too many redirects")
                if response.status_code in (401, 403):
                    response.close()
                    raise AuthError("Authentication expired or access denied; update local credentials")
                if response.status_code == 429 or response.status_code >= 500:
                    response.close()
                    if attempt < self.retries:
                        time.sleep(min(2 ** attempt, 20))
                        continue
                if response.status_code >= 400:
                    code = response.status_code
                    response.close()
                    raise ApiError(f"HTTP {code}")
                return response
            except requests.RequestException:
                if attempt == self.retries:
                    raise ApiError("Network request failed (URL and credentials redacted)") from None
                time.sleep(min(2 ** attempt, 20))
        raise ApiError("Retry budget exhausted")

    def api(self, path, **params):
        url = path if path.startswith("https://") else self.server + path
        with self.request(url, params=params) as response:
            try:
                data = response.json()
            except ValueError:
                raise ApiError("API returned invalid JSON") from None
        code = data.get("returnCode", data.get("return_code"))
        if code not in (None, 200):
            if code in (301, 401, 403):
                raise AuthError(f"API authentication/access error {code}")
            raise ApiError(f"API return code {code}")
        return data

    def version_list(self, object_type, *, page_size=100):
        cursor = 0
        seen = set()
        while True:
            data = self.api(f"/ks/{object_type}/list/version/{self.kb}",
                            version=cursor, count=page_size, pageSize=page_size, withAbstract="false")
            rows = data.get("result")
            if not isinstance(rows, list):
                raise ApiError(f"Invalid {object_type} inventory response")
            if not rows:
                return
            versions = [int(row["version"]) for row in rows]
            if any(v < cursor for v in versions):
                raise ApiError("Inventory returned an old version below the cursor")
            next_cursor = max(versions) + 1
            fingerprint = json.dumps(rows, sort_keys=True)
            if fingerprint in seen or next_cursor <= cursor:
                raise ApiError("Inventory pagination did not advance; refusing incomplete success")
            seen.add(fingerprint)
            yield rows
            cursor = next_cursor

    def download(self, url, target, *, expected_md5=None):
        import hashlib
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        # Completed objects resume by hash; interrupted objects restart atomically.
        for attempt in range(self.retries + 1):
            try:
                digest = hashlib.md5()
                with self.request(url, stream=True) as response, part.open("wb") as out:
                    os.chmod(part, 0o600)
                    expected_length = response.headers.get("Content-Length")
                    size = 0
                    for chunk in response.iter_content(1024 * 1024):
                        out.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                    if expected_length and not response.headers.get("Content-Encoding") and size != int(expected_length):
                        raise ApiError("Downloaded size mismatch")
                if expected_md5 and digest.hexdigest().lower() != expected_md5.lower():
                    raise ApiError("Downloaded MD5 mismatch")
                part.replace(target)
                return size
            except (requests.RequestException, ApiError) as exc:
                if isinstance(exc, AuthError) or attempt == self.retries:
                    if isinstance(exc, requests.RequestException):
                        raise ApiError("Interrupted object download") from None
                    raise
                time.sleep(min(2 ** attempt, 20))
