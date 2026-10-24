"""Synthetic archival regression tests; no real account or note data is read."""
from __future__ import annotations

import copy
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from wizbackup.backup import note_path, process_note, run_backup, sha256, valid_record, verify


NOTE = {"docGuid": "11111111-1111-4111-8111-111111111111", "kbGuid": "test-kb",
        "version": 11, "title": "Synthetic note", "category": "/Folder/",
        "protected": 0, "attachmentCount": 0, "type": "document"}
AUTH = {"token": "synthetic-test-token", "kb_guid": "test-kb",
        "kb_server": "https://test.wiz.cn"}


def zip_bytes(name="index.html", body=b"<html><body>Test note</body></html>"):
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, body)
    return out.getvalue()


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "metadata").mkdir()
        (self.root / "records").mkdir()
        (self.root / "metadata/notes.json").write_text(json.dumps([NOTE]))
        self.obj = self.root / "objects" / NOTE["docGuid"] / str(NOTE["version"])
        self.obj.mkdir(parents=True)
        (self.obj / "original.ziw").write_bytes(zip_bytes())
        (self.obj / "metadata.json").write_text(json.dumps(NOTE))
        self.record = {"guid": NOTE["docGuid"], "version": NOTE["version"], "status": "exported",
                       "warnings": [], "export": {"status": "exported", "warnings": [],
                                                   "missing_resources": [], "external_resources": []},
                       "files": []}
        self.record_path = self.root / "records" / (NOTE["docGuid"] + ".json")
        self.refresh_files()

    def refresh_files(self):
        self.record["files"] = [{"path": str(p.relative_to(self.root)), "size": p.stat().st_size,
                                 "sha256": sha256(p)} for p in sorted(self.obj.iterdir())]
        self.save()

    def save(self):
        self.record_path.write_text(json.dumps(self.record))

    def test_complete_current_record_passes(self):
        result = verify(self.root)
        self.assertTrue(result["integrity_ok"])
        self.assertTrue(result["all_readable"])

    def test_cached_attachments_require_complete_new_validations_and_matching_hashes(self):
        self.record["attachments"] = 0
        self.assertTrue(valid_record(self.root, self.record, NOTE))
        self.record["attachments"] = 1
        self.assertFalse(valid_record(self.root, self.record, NOTE))
        self.record["attachment_validations"] = []
        self.assertFalse(valid_record(self.root, self.record, NOTE))
        self.record["attachment_validations"] = [{"attGuid": "synthetic-attachment", "checksum": "verified"}]
        self.assertTrue(valid_record(self.root, self.record, NOTE))
        (self.obj / "original.ziw").write_bytes(b"corrupt cached body")
        self.assertFalse(valid_record(self.root, self.record, NOTE))

    def test_failed_unsupported_and_empty_files_cannot_pass_integrity(self):
        for status, files in [("failed", self.record["files"]),
                              ("unsupported", self.record["files"]), ("exported", [])]:
            with self.subTest(status=status, empty_files=not files):
                self.record.update(status=status, files=files)
                self.save()
                result = verify(self.root)
                self.assertFalse(result["integrity_ok"])
                self.assertFalse(result["all_readable"])

    def test_stale_record_cannot_cover_new_inventory_version(self):
        self.record["version"] = NOTE["version"] - 1
        self.save()
        result = verify(self.root)
        self.assertFalse(result["integrity_ok"])
        self.assertIn(NOTE["docGuid"], result["stale_records"])

    def test_missing_record_is_reported(self):
        self.record_path.rename(self.record_path.with_suffix(".not-a-record"))
        result = verify(self.root)
        self.assertFalse(result["integrity_ok"])
        self.assertIn(NOTE["docGuid"], result["notes_without_record"])

    def test_corrupt_file_cannot_pass_integrity(self):
        (self.obj / "original.ziw").write_bytes(b"corrupt")
        result = verify(self.root)
        self.assertFalse(result["integrity_ok"])
        self.assertTrue(result["corrupt_files"])

    def test_export_issues_block_readable_success_even_without_record_warnings(self):
        for key in ("warnings", "missing_resources", "external_resources"):
            with self.subTest(key=key):
                self.record["export"] = {"status": "exported", key: ["synthetic issue"]}
                self.record["warnings"] = []
                self.save()
                result = verify(self.root)
                self.assertTrue(result["integrity_ok"])
                self.assertFalse(result["all_readable"])

    def test_record_warnings_block_readable_success(self):
        self.record["warnings"] = ["History fetch unavailable"]
        self.save()
        self.assertFalse(verify(self.root)["all_readable"])

    def test_locked_original_is_preserved_but_not_readable(self):
        self.record["status"] = "locked"
        self.save()
        result = verify(self.root)
        self.assertTrue(result["integrity_ok"])
        self.assertFalse(result["all_readable"])

    def test_record_guid_must_match_its_filename(self):
        self.record["guid"] = "different-guid"
        self.save()
        self.assertFalse(verify(self.root)["integrity_ok"])

    def test_hash_valid_wrong_metadata_guid_kb_or_version_is_rejected(self):
        for field, value in [("docGuid", "different-guid"), ("kbGuid", "different-kb"),
                             ("version", NOTE["version"] - 1)]:
            with self.subTest(field=field):
                metadata = {**NOTE, field: value}
                (self.obj / "metadata.json").write_text(json.dumps(metadata))
                self.refresh_files()
                self.assertFalse(verify(self.root)["integrity_ok"])


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = MagicMock()
        self.client.kb = AUTH["kb_guid"]
        self.client.server = AUTH["kb_server"]

    def process(self, note=NOTE, *, root=None):
        with patch("wizbackup.backup.WizClient", return_value=self.client):
            return process_note(AUTH, "direct", root or self.root, copy.deepcopy(note), supplemental=False)

    def test_original_single_member_zip_attachment_is_not_unwrapped(self):
        original = zip_bytes("user-file.txt", b"User's original zipped content")
        attachment = {"attGuid": "attachment-guid", "name": "original.zip",
                      "dataSize": len(original), "dataMd5": hashlib.md5(original).hexdigest()}
        note = {**NOTE, "attachmentCount": 1}
        self.client.api.side_effect = [{"info": note, "html": "<p>Body</p>", "resources": []},
                                       {"result": [attachment]}]

        def download(_url, target, **_kwargs):
            target = Path(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(original)
            return len(original)

        self.client.download.side_effect = download
        record, reused = self.process(note)
        self.assertFalse(reused)
        self.assertEqual(record["status"], "exported")
        local = self.root / note_path(note) / "attachments/attachment-guid--original.zip"
        self.assertEqual(local.read_bytes(), original)
        self.assertTrue(zipfile.is_zipfile(local))

    def test_duplicate_resource_descriptors_do_not_create_duplicate_zip_entries(self):
        image = b"synthetic-image"
        resource = {"name": "image.png", "url": "https://test.wiz.cn/image", "size": len(image), "time": 1}
        self.client.api.side_effect = [
            {"info": NOTE, "html": '<p>Body</p><img src="index_files/image.png">',
             "resources": [resource, {**resource, "time": 2}]}, {"result": []}]
        def download(_url, target, **_kwargs):
            target = Path(target); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(image)
        self.client.download.side_effect = download
        record, _ = self.process()
        self.assertEqual(record["status"], "exported")
        self.assertEqual(record["duplicate_resource_descriptors"], 1)
        self.client.download.assert_called_once()
        package = self.root / "objects" / NOTE["docGuid"] / str(NOTE["version"]) / "content.ziw"
        with zipfile.ZipFile(package) as z:
            self.assertEqual(z.namelist().count("index_files/image.png"), 1)

    def test_checksum_verified_body_accepts_stale_size_with_audit_evidence(self):
        original = zip_bytes()
        note = {**NOTE, "dataSize": 100, "dataMd5": hashlib.md5(original).hexdigest()}
        self.client.api.side_effect = [{"info": note, "url": "https://test.wiz.cn/body"}, {"result": []}]
        self.client.download.side_effect = lambda _url, target, **_kw: Path(target).write_bytes(original)
        record, _ = self.process(note)
        self.assertEqual(record["status"], "exported")
        self.assertEqual(record["source_metadata_anomalies"][0]["kind"], "body_size")

    def test_encrypted_server_md5_anomaly_is_recovered_but_never_reported_as_checksum_success(self):
        note = {**NOTE, "protected": 1, "dataSize": 100, "dataMd5": "0" * 32}
        payload = {"info": note, "url": "https://test.wiz.cn/encrypted"}
        self.client.api.side_effect = [payload, payload, {"result": []}]
        self.client.download.side_effect = lambda _url, target, **_kw: Path(target).write_bytes(b"synthetic-ciphertext")
        (self.root / "metadata").mkdir()
        (self.root / "metadata/user-cert.json").write_text('{"result": {}}')
        password = self.root / "synthetic-password-file"
        password.write_text("synthetic-password")
        def decrypt(_raw, dest, _cert, _password): Path(dest).write_bytes(zip_bytes())
        with patch("wizbackup.backup.WizClient", return_value=self.client), patch("wizbackup.crypto.decrypt_ziw", side_effect=decrypt):
            rec, _ = process_note(AUTH, "direct", self.root, note, supplemental=False, password_file=password)
        self.assertEqual(rec["status"], "exported")
        self.assertEqual(rec["original_checksum"], "server-md5-mismatch")
        self.assertTrue(rec["source_checksum_anomaly"])
        self.assertTrue(rec["warnings"])
        self.assertTrue(rec["decrypted_locally"])

    def test_checksum_anomaly_recovery_rejects_a_changing_server_object(self):
        note = {**NOTE, "protected": 1, "dataMd5": "0" * 32}
        payload = {"info": note, "url": "https://test.wiz.cn/encrypted"}
        self.client.api.side_effect = [payload, payload]
        counter = iter([b"first-ciphertext", b"different-ciphertext"])
        self.client.download.side_effect = lambda _url, target, **_kw: Path(target).write_bytes(next(counter))
        password = self.root / "synthetic-password-file"; password.write_text("synthetic-password")
        with patch("wizbackup.backup.WizClient", return_value=self.client), patch("wizbackup.crypto.decrypt_ziw") as decrypt:
            rec, _ = process_note(AUTH, "direct", self.root, note, supplemental=False, password_file=password)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("changed", rec["error"])
        decrypt.assert_not_called()

    def test_stable_css_accepts_stale_size_but_keeps_both_response_hashes(self):
        body = b"img { max-width: 100%; }"
        self.client.api.side_effect = [{"info": NOTE, "html": '<p>Body</p><link rel="stylesheet" href="index_files/style.css">',
            "resources": [{"name": "style.css", "url": "https://test.wiz.cn/style", "size": 100}]}, {"result": []}]
        def download(_url, target, **_kw):
            target = Path(target); target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(body)
        self.client.download.side_effect = download
        record, _ = self.process()
        self.assertEqual(record["status"], "exported")
        self.assertEqual(self.client.download.call_count, 2)
        self.assertEqual(record["source_metadata_anomalies"][0]["kind"], "css_size")

    def test_corrupt_cached_original_is_downloaded_again_before_recertification(self):
        original = zip_bytes()
        obj = self.root / "objects" / NOTE["docGuid"] / str(NOTE["version"])
        obj.mkdir(parents=True)
        cached = obj / "original.ziw"
        cached.write_bytes(b"corrupted cached object")
        records = self.root / "records"
        records.mkdir()
        (records / (NOTE["docGuid"] + ".json")).write_text(json.dumps({
            "guid": NOTE["docGuid"], "version": NOTE["version"], "status": "exported", "warnings": [],
            "files": [{"path": str(cached.relative_to(self.root)), "size": len(original),
                       "sha256": hashlib.sha256(original).hexdigest()}],
        }))
        self.client.api.side_effect = [{"info": NOTE, "url": "https://test.wiz.cn/original"},
                                       {"result": []}]

        def download(_url, target, **_kwargs):
            Path(target).write_bytes(original)
            return len(original)

        self.client.download.side_effect = download
        record, reused = self.process()
        self.assertFalse(reused)
        self.assertEqual(record["status"], "exported")
        self.client.download.assert_called_once()
        self.assertEqual(cached.read_bytes(), original)

    def test_payload_identity_and_version_must_match_inventory(self):
        for field, value in [("docGuid", "different-guid"), ("kbGuid", "different-kb"),
                             ("version", NOTE["version"] - 1)]:
            with self.subTest(field=field):
                self.client.api.reset_mock(side_effect=True)
                self.client.api.side_effect = [{"info": {**NOTE, field: value},
                                                "html": "<p>Wrong identity body</p>", "resources": []},
                                               {"result": []}]
                case_root = self.root / field
                case_root.mkdir()
                record, reused = self.process(root=case_root)
                self.assertFalse(reused)
                self.assertEqual(record["status"], "failed")

    def test_attachment_count_mismatch_is_failure(self):
        self.client.api.side_effect = [{"info": {**NOTE, "attachmentCount": 1},
                                        "html": "<p>Body</p>", "resources": []}, {"result": []}]
        record, _ = self.process()
        self.assertEqual(record["status"], "failed")

    def test_empty_inventory_does_not_crash(self):
        with patch("wizbackup.backup.WizClient", return_value=self.client), \
                patch("wizbackup.backup.inventory", return_value=[]):
            state = run_backup(AUTH, self.root, workers=1)
        self.assertEqual(state["total"], 0)
        self.assertEqual(state["completed"], 0)
        self.assertEqual(state["phase"], "downloaded")


if __name__ == "__main__":
    unittest.main()
