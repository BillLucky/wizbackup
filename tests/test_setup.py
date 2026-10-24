"""Onboarding must not expose or retain a login password."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from wizbackup.client import ApiError
from wizbackup.setup import configure, _login

AUTH = {"token": "synthetic-local-session-token", "kb_guid": "11111111-1111-4111-8111-111111111111", "kb_server": "https://synthetic-ks.wiz.cn"}

class SetupTests(unittest.TestCase):
    def test_local_import_has_private_modes_and_no_login_password(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / "input.json"
            source.write_text(json.dumps({**AUTH, "password": "synthetic-login-password"})); source.chmod(0o600)
            original = source.read_bytes()
            result = configure(root / "private-data", from_auth=source, proxy="http://127.0.0.1:8123")
            saved = Path(result["auth_file"])
            self.assertEqual(json.loads(saved.read_text()), AUTH)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
            self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(source.read_bytes(), original)
            self.assertIn("--proxy", result["next_command"])
            self.assertNotIn("synthetic-login-password", json.dumps(result))

    def test_existing_auth_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); source = root / "input.json"; source.write_text(json.dumps(AUTH)); source.chmod(0o600)
            result = configure(root / "data", from_auth=source)
            old = Path(result["auth_file"]).read_bytes()
            with self.assertRaises(ApiError): configure(root / "data", from_auth=source)
            self.assertEqual(Path(result["auth_file"]).read_bytes(), old)

    def test_noninteractive_setup_never_prompts_or_echoes_secrets(self):
        with tempfile.TemporaryDirectory() as folder, patch("wizbackup.setup.sys.stdin.isatty", return_value=False), patch("wizbackup.setup.getpass.getpass") as prompt:
            with self.assertRaisesRegex(ApiError, "local terminal"): configure(Path(folder) / "data")
            prompt.assert_not_called()

    def test_normal_login_uses_only_official_tls_endpoint(self):
        response = MagicMock(status_code=200); response.json.return_value = {"returnCode": 200, "result": {"token": AUTH["token"], "kb_guid": AUTH["kb_guid"], "kapi_url": AUTH["kb_server"]}}
        session = MagicMock(); session.post.return_value = response
        with patch("wizbackup.setup.requests.Session", return_value=session):
            result = _login("synthetic-account", "synthetic-password")
        self.assertEqual(result, AUTH)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.post.call_args.args[0], "https://as.wiz.cn/as/user/login")
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])
        self.assertNotIn("password", result)
        session.close.assert_called_once()

    def test_verification_required_does_not_disable_mfa_or_echo_response(self):
        response = MagicMock(status_code=200); response.json.return_value = {"returnCode": 200, "result": {"verification": "synthetic-sensitive-response"}}
        session = MagicMock(); session.post.return_value = response
        with patch("wizbackup.setup.requests.Session", return_value=session), self.assertRaises(ApiError) as error:
            _login("synthetic-account", "synthetic-password")
        self.assertNotIn("synthetic-sensitive-response", str(error.exception))
        self.assertNotIn("synthetic-password", str(error.exception))

    def test_source_checkout_cannot_be_a_data_destination(self):
        with self.assertRaisesRegex(ApiError, "outside the software checkout"):
            configure(Path(__file__).resolve().parents[1] / "backups")
