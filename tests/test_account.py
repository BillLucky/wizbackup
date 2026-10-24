"""Account orchestration regressions; synthetic credentials and mocked I/O."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from wizbackup.account import discover, run_account
from wizbackup.client import ApiError, AuthError


PERSONAL = "11111111-1111-4111-8111-111111111111"
TEAM = "22222222-2222-4222-8222-222222222222"
AUTH = {"token": "synthetic-account-test-token", "kb_guid": PERSONAL,
        "kb_server": "https://personal-test.wiz.cn"}
BASES = [
    {"kb_guid": PERSONAL, "kb_server": AUTH["kb_server"], "kind": "personal", "path": "personal"},
    {"kb_guid": TEAM, "kb_server": "https://team-test.wiz.cn", "kind": "team", "path": "teams/" + TEAM},
]
DONE = {"phase": "downloaded", "total": 1, "completed": 1, "counts": {"exported": 1}}
FAILED = {"phase": "needs_retry", "total": 1, "completed": 1, "counts": {"failed": 1}}
AUTH_NEEDED = {"phase": "needs_retry", "total": 1, "completed": 1, "counts": {"auth_required": 1}}


class DiscoverTests(unittest.TestCase):
    def client(self, payload):
        client = MagicMock()
        client.api.return_value = payload
        return client

    def test_current_group_inventory_is_deduplicated_and_credentials_are_not_returned(self):
        client = self.client({"result": {"groupList": [
            {"kbGuid": PERSONAL, "kbServer": "https://ignored-duplicate.wiz.cn"},
            {"kbGuid": TEAM, "kbServer": "https://team-test.wiz.cn", "type": "team",
             "token": "synthetic-group-secret", "cookie": "synthetic-cookie", "name": "Synthetic team"},
            {"kbGuid": TEAM, "kbServer": "https://ignored-duplicate.wiz.cn"},
            {"kbServer": "https://no-guid.wiz.cn"},
        ]}})
        original = copy.deepcopy(AUTH)
        with patch("wizbackup.account.WizClient", return_value=client) as constructor:
            bases = discover(AUTH, network="us")
        constructor.assert_called_once_with(AUTH, "us")
        client.api.assert_called_once_with("https://as.wiz.cn/as/groups")
        client.session.close.assert_called_once()
        self.assertEqual([PERSONAL, TEAM], [base["kb_guid"] for base in bases])
        self.assertEqual(["personal", "teams/" + TEAM], [base["path"] for base in bases])
        self.assertEqual("https://team-test.wiz.cn", bases[1]["kb_server"])
        serialized = json.dumps(bases)
        for secret in (AUTH["token"], "synthetic-group-secret", "synthetic-cookie"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(original, AUTH)

    def test_list_shaped_group_inventory_is_supported_without_historical_fallback(self):
        client = self.client({"result": [{"kbGuid": TEAM, "kbServer": "https://team-test.wiz.cn"}]})
        with patch("wizbackup.account.WizClient", return_value=client):
            self.assertEqual(2, len(discover(AUTH)))
        client.api.return_value = {"result": {"groupList": []}}
        client.session.close.reset_mock()
        with patch("wizbackup.account.WizClient", return_value=client):
            bases = discover(AUTH)
        self.assertEqual([PERSONAL], [base["kb_guid"] for base in bases])
        client.session.close.assert_called_once()

    def test_new_library_without_server_is_rejected_and_session_is_closed(self):
        client = self.client({"result": {"groupList": [{"kbGuid": TEAM}]}})
        with patch("wizbackup.account.WizClient", return_value=client):
            with self.assertRaisesRegex(ApiError, "server"):
                discover(AUTH)
        client.session.close.assert_called_once()

    def test_discovery_auth_error_propagates_instead_of_using_stale_libraries(self):
        client = self.client({})
        client.api.side_effect = AuthError("Synthetic expired authentication")
        with patch("wizbackup.account.WizClient", return_value=client):
            with self.assertRaises(AuthError):
                discover(AUTH)
        client.session.close.assert_called_once()


class AccountFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = (Path(self.temp.name) / "account").resolve()

    def start_patch(self, target, **kwargs):
        patcher = patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def mock_flow(self, states, *, bases=BASES):
        discover_mock = None
        if bases is not None:
            discover_mock = self.start_patch("wizbackup.account.discover", return_value=copy.deepcopy(bases))
        backup = self.start_patch("wizbackup.account.run_backup", side_effect=copy.deepcopy(states))
        verification = self.start_patch("wizbackup.account.verify", side_effect=lambda _root: {"integrity_ok": True, "all_readable": True})
        packing = self.start_patch("wizbackup.account.pack", side_effect=lambda _root, dest: {"archive": str(dest), "crc_ok": True})
        # These stages are independently tested in their own suites. Patch both
        # module and optional account aliases so account integration can import
        # them either at module scope or locally without requiring real notes.
        for target in ("wizbackup.catalog.finish_catalog", "wizbackup.account.finish_catalog"):
            self.start_patch(target, return_value={"ok": True, "rewritten_files": 0}, create=True)
        for target in ("wizbackup.external.mirror_external", "wizbackup.account.mirror_external"):
            self.start_patch(target, return_value={"resources_failed": 0, "skipped_invalid_records": 0}, create=True)
        return SimpleNamespace(discover=discover_mock, backup=backup, verify=verification, pack=packing)

    def test_each_library_gets_its_own_auth_server_path_and_private_manifest(self):
        mocks = self.mock_flow([DONE, DONE], bases=None)
        client = MagicMock()
        client.api.return_value = {"result": {"groupList": [
            {"kbGuid": TEAM, "kbServer": "https://team-test.wiz.cn", "type": "team",
             "token": "synthetic-group-secret", "name": "Synthetic team"}
        ]}}
        original = copy.deepcopy(AUTH)
        password = Path(self.temp.name) / "synthetic-password-file"
        with patch("wizbackup.account.WizClient", return_value=client):
            reports = run_account(AUTH, self.root, network="us", workers=3, password_file=password, archives=False)
        self.assertEqual(2, len(reports))
        self.assertEqual(2, mocks.backup.call_count)
        for call, base in zip(mocks.backup.call_args_list, BASES):
            local_auth, destination = call.args
            self.assertEqual(base["kb_guid"], local_auth["kb_guid"])
            self.assertEqual(base["kb_server"], local_auth["kb_server"])
            self.assertEqual(AUTH["token"], local_auth["token"])
            self.assertEqual(self.root / base["path"], destination)
            self.assertEqual("us", call.kwargs["network"])
            self.assertEqual(3, call.kwargs["workers"])
            self.assertEqual(password, call.kwargs["password_file"])
        self.assertNotEqual(mocks.backup.call_args_list[0].args[0], mocks.backup.call_args_list[1].args[0])
        self.assertEqual(original, AUTH)
        manifest = (self.root / "knowledge-bases.json").read_text()
        self.assertNotIn(AUTH["token"], manifest)
        self.assertNotIn("synthetic-group-secret", manifest)
        self.assertNotIn('"token"', manifest)
        self.assertEqual(0o600, (self.root / "knowledge-bases.json").stat().st_mode & 0o777)
        self.assertEqual(2, len(json.loads((self.root / "account-summary.json").read_text())))
        mocks.pack.assert_not_called()

    def test_no_refresh_reuses_existing_inventory_but_scans_new_library(self):
        existing = self.root / "personal/metadata/notes.json"
        existing.parent.mkdir(parents=True)
        existing.write_text("[]")
        mocks = self.mock_flow([DONE, DONE])
        run_account(AUTH, self.root, refresh=False, archives=False)
        self.assertFalse(mocks.backup.call_args_list[0].kwargs["refresh"])
        self.assertTrue(mocks.backup.call_args_list[1].kwargs["refresh"])
        self.assertEqual("[]", existing.read_text())

    def test_failed_notes_get_one_bounded_retry_without_refreshing_inventory(self):
        mocks = self.mock_flow([FAILED, FAILED, DONE])
        reports = run_account(AUTH, self.root, archives=False)
        self.assertEqual(3, mocks.backup.call_count)
        self.assertEqual([PERSONAL, PERSONAL, TEAM], [call.args[0]["kb_guid"] for call in mocks.backup.call_args_list])
        self.assertEqual([True, False, True], [call.kwargs["refresh"] for call in mocks.backup.call_args_list])
        self.assertEqual("needs_retry", reports[0]["backup"]["phase"])

    def test_mid_download_auth_failure_is_not_retried_with_same_expired_token(self):
        mocks = self.mock_flow([AUTH_NEEDED, DONE])
        reports = run_account(AUTH, self.root, archives=False)
        personal_calls = [call for call in mocks.backup.call_args_list if call.args[0]["kb_guid"] == PERSONAL]
        self.assertEqual(1, len(personal_calls))
        self.assertEqual(1, reports[0]["backup"]["counts"]["auth_required"])

    def test_archives_are_siblings_outside_account_tree_and_library_subtrees(self):
        mocks = self.mock_flow([DONE, DONE])
        with patch("wizbackup.account.time.strftime", return_value="20261023-235959"):
            reports = run_account(AUTH, self.root, archives=True)
        self.assertEqual(2, mocks.pack.call_count)
        destinations = []
        for call, base in zip(mocks.pack.call_args_list, BASES):
            source, destination = call.args
            self.assertEqual(self.root / base["path"], source)
            self.assertFalse(destination.is_relative_to(self.root))
            self.assertEqual(self.root.parent / "archives", destination.parent)
            self.assertTrue(destination.name.endswith("20261023-235959.zip"))
            destinations.append(destination)
        self.assertEqual(2, len(set(destinations)))
        self.assertTrue(all(report["archive"]["crc_ok"] for report in reports))


if __name__ == "__main__":
    unittest.main()
