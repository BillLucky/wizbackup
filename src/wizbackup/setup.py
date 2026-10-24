"""Local credential onboarding; passwords are never command arguments or logs."""
from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
import stat
import sys
import uuid
from urllib.parse import urlsplit

import requests

from .client import ApiError, is_wiz_host, normalized_proxy


def _validated_auth(value):
    if not isinstance(value, dict): raise ApiError("Authorization file must contain a JSON object")
    try:
        token, guid, server = value["token"], value["kb_guid"], value["kb_server"].rstrip("/")
        guid = str(uuid.UUID(guid))
        parts = urlsplit(server)
        if (not isinstance(token, str) or not token.strip() or parts.scheme != "https"
                or not is_wiz_host(parts.hostname) or parts.username or parts.password
                or parts.path or parts.query or parts.fragment or parts.port not in (None, 443)):
            raise ValueError
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ApiError("Authorization needs a token, a valid KB GUID, and your official Wiz HTTPS KB server") from None
    return {"token": token.strip(), "kb_guid": guid, "kb_server": server}


def _login(account, password, proxy=None):
    """Normal sign-in only. Verification/CAPTCHA must remain with the user."""
    session = requests.Session()
    session.trust_env = False
    selected = normalized_proxy(proxy)
    session.proxies = {"http": selected, "https": selected} if selected else {}
    try:
        response = session.post("https://as.wiz.cn/as/user/login", data={
            "user_id": account, "password": password, "client_type": "web", "client_version": "4.0.0"},
            timeout=(15, 30), allow_redirects=False)
        try:
            if response.status_code != 200: raise ApiError("Sign-in failed; use the official website and session-token method")
            payload = response.json()
        finally: response.close()
        if payload.get("returnCode", payload.get("return_code")) != 200:
            raise ApiError("Sign-in needs verification or was rejected; use the official website and session-token method")
        result = payload.get("result", payload)
        return _validated_auth({"token": result.get("token"), "kb_guid": result.get("kbGuid", result.get("kb_guid")),
                                "kb_server": result.get("kapi_url") or result.get("kbServer") or result.get("kb_server")})
    except requests.RequestException:
        raise ApiError("Sign-in network error (credentials and response body redacted)") from None
    except (ValueError, TypeError, AttributeError):
        raise ApiError("Sign-in returned no usable session; use the official website and session-token method") from None
    finally: session.close()


def configure(data_dir: Path, *, from_auth: Path | None = None, proxy=None):
    """Create private local authorization outside the source checkout."""
    selected_proxy = normalized_proxy(proxy)
    root = Path(data_dir).expanduser().resolve()
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "pyproject.toml").exists() and (root == checkout or checkout in root.parents):
        raise ApiError("Choose a data directory outside the software checkout")
    if Path(data_dir).expanduser().is_symlink(): raise ApiError("Data directory must not be a symlink")
    private = root / "private"
    if private.is_symlink(): raise ApiError("Private configuration directory must not be a symlink")
    auth_file, password_file = private / "auth.json", private / "decryption-password.txt"
    if auth_file.exists() or auth_file.is_symlink(): raise ApiError("Authorization already exists; update the private file or choose a new data directory")
    encryption_password = None
    if from_auth is not None:
        source = Path(from_auth)
        if source.is_symlink() or not source.is_file() or stat.S_IMODE(source.stat().st_mode) & 0o077:
            raise ApiError("Existing authorization file must be a regular private file with mode 600")
        auth = _validated_auth(json.loads(source.read_text()))
    else:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise ApiError("Run setup in a local terminal, or use --from-auth with a private local file; do not paste passwords into agent chat")
        method = input("Authorization: [1] existing web session (default), [2] account/password: ").strip()
        if method == "2":
            account = input("Wiz account: ").strip()
            password = getpass.getpass("Wiz login password (not saved): ")
            try: auth = _login(account, password, selected_proxy)
            finally: password = None
        else:
            token = getpass.getpass("x-wiz-token (hidden): ")
            server = input("Your HTTPS KB server from the web session: ").strip()
            guid = input("Your personal KB GUID from the web session: ").strip()
            auth = _validated_auth({"token": token, "kb_server": server, "kb_guid": guid})
            token = None
        encryption_password = getpass.getpass("Local note encryption password, if any (optional, hidden): ") or None
    if encryption_password and (password_file.exists() or password_file.is_symlink()):
        raise ApiError("An encryption password file already exists; it was not changed")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    private.mkdir(exist_ok=True, mode=0o700)
    private.chmod(0o700)
    def create(path, data):
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
            out.write(data); out.flush(); os.fsync(out.fileno())
    create(auth_file, json.dumps(auth, ensure_ascii=False, indent=2) + "\n")
    if encryption_password:
        create(password_file, encryption_password + "\n")
    encryption_password = None
    command = ["wizbackup", "backup-all", "--auth", str(auth_file), "--output", str(root / "backups/account"),
               "--password-file", str(password_file)]
    if selected_proxy: command += ["--proxy", selected_proxy]
    return {"ok": True, "data_directory": str(root), "auth_file": str(auth_file),
            "password_file": str(password_file) if password_file.exists() else None,
            "next_command": command}
