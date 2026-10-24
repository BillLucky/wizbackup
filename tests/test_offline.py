"""Offline recovery integration tests using generated, synthetic archives."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile
from urllib.parse import unquote

from bs4 import BeautifulSoup

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wizbackup.backup import pack, sha256
from wizbackup.client import write_json
from wizbackup.offline import (OfflineError, inspect_archive, restore_archive,
                               rebuild_readable, export_vault)

_DEMO_PATH = Path(__file__).parents[1] / "examples/create_demo.py"
_SPEC = importlib.util.spec_from_file_location("synthetic_demo", _DEMO_PATH)
_DEMO = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_DEMO)


def snapshot(root):
    return {p.relative_to(root).as_posix(): sha256(p) for p in root.rglob("*") if p.is_file()}


def aes_encrypt(data, password):
    pad = padding.PKCS7(128).padder()
    padded = pad.update(data) + pad.finalize()
    key = hashlib.md5(password, usedforsecurity=False).hexdigest().encode()
    cipher = Cipher(algorithms.AES(key), modes.CBC(b"0123456789abcdef")).encryptor()
    return cipher.update(padded) + cipher.finalize()


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.library = _DEMO.create_library(self.base / "source")
        self.record_path = next((self.library / "records").glob("*.json"))
        self.record = json.loads(self.record_path.read_text())
        self.note = json.loads((self.library / "metadata/notes.json").read_text())[0]
        self.obj = self.library / "objects" / self.note["docGuid"] / "1"

    def write_record(self):
        write_json(self.record_path, self.record)

    def archive(self):
        output = self.base / "source.zip"
        pack(self.library, output)
        return output

    def generic_zip(self, entries):
        path = self.base / "generic.zip"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zipped:
                for name, value in entries:
                    zipped.writestr(name, value)
        return path

    def test_every_operation_is_offline_and_source_is_unchanged(self):
        source_before = snapshot(self.library)
        archive = self.archive()
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
             patch("socket.create_connection", side_effect=AssertionError("Network forbidden")), \
             patch("requests.Session", side_effect=AssertionError("Network forbidden")):
            info = inspect_archive(self.library)
            zipped_info = inspect_archive(archive)
            restored = restore_archive(archive, self.base / "restored")
            rebuilt = rebuild_readable(self.library, self.base / "rebuilt")
            vault = export_vault(self.library, self.base / "vault")
        self.assertTrue(info["ok"])
        self.assertTrue(zipped_info["sha256_sidecar_verified"])
        self.assertTrue(restored["local_integrity_ok"])
        self.assertTrue(restored["all_readable"])
        self.assertTrue(rebuilt["all_readable"])
        self.assertTrue(vault["all_readable"])
        self.assertEqual(source_before, snapshot(self.library))

    def test_account_restore_detects_both_isolated_libraries(self):
        output = self.base / "demo"
        _DEMO.create_demo(output)
        result = restore_archive(output / "synthetic-account.zip", self.base / "restored-account")
        self.assertEqual(2, len(result["libraries"]))
        self.assertTrue(all(row["local_integrity_ok"] for row in result["libraries"]))

    def test_nonempty_outputs_are_never_modified(self):
        archive = self.archive()
        for function, source in [(restore_archive, archive), (rebuild_readable, self.library), (export_vault, self.library)]:
            with self.subTest(operation=function.__name__):
                destination = self.base / function.__name__
                destination.mkdir()
                (destination / "keep.txt").write_bytes(b"keep")
                with self.assertRaises(OfflineError):
                    function(source, destination)
                self.assertEqual({"keep.txt": hashlib.sha256(b"keep").hexdigest()}, snapshot(destination))

    def test_destinations_cannot_overlap_source_tree(self):
        for function in (rebuild_readable, export_vault):
            with self.subTest(operation=function.__name__):
                with self.assertRaises(OfflineError):
                    function(self.library, self.library / "nested-output")
        self.assertFalse((self.library / "nested-output").exists())

    def test_traversal_absolute_windows_and_control_paths_are_rejected(self):
        for name in ("../escape.txt", "/absolute.txt", "C:/drive.txt", "folder\\escape.txt", "safe/../escape.txt", "bad\nname"):
            with self.subTest(name=name):
                archive = self.generic_zip([(name, b"x")])
                with self.assertRaises(OfflineError):
                    restore_archive(archive, self.base / "unsafe-output")
                self.assertFalse((self.base / "unsafe-output").exists())
        self.assertFalse((self.base / "escape.txt").exists())

    def test_symlink_zip_members_are_rejected(self):
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive = self.generic_zip([(link, b"outside")])
        with self.assertRaises(OfflineError):
            restore_archive(archive, self.base / "links-output")

    def test_file_directory_and_unicode_case_collisions_are_rejected(self):
        for entries in [
            [("node", b"x"), ("node/child", b"y")],
            [("A.txt", b"x"), ("a.txt", b"y")],
            [("café.txt", b"x"), ("cafe\u0301.txt", b"x")],
        ]:
            with self.subTest(entries=len(entries)):
                with self.assertRaises(OfflineError):
                    restore_archive(self.generic_zip(entries), self.base / "collision-output")

    def test_identical_duplicates_are_reported_and_conflicting_duplicates_refused(self):
        archive = self.generic_zip([("same.txt", b"same"), ("same.txt", b"same")])
        result = restore_archive(archive, self.base / "same-output")
        self.assertEqual(1, result["duplicate_identical_entries"])
        self.assertEqual(b"same", (self.base / "same-output/same.txt").read_bytes())
        archive = self.generic_zip([("same.txt", b"first"), ("same.txt", b"other")])
        with self.assertRaises(OfflineError):
            restore_archive(archive, self.base / "different-output")

    def test_limits_fail_before_destination_is_published(self):
        archive = self.generic_zip([("payload.bin", b"1234")])
        for limit in ("MAX_ENTRIES", "MAX_UNCOMPRESSED", "MAX_ENTRY_BYTES"):
            with self.subTest(limit=limit), patch("wizbackup.offline." + limit, 0):
                with self.assertRaises(OfflineError):
                    restore_archive(archive, self.base / "limit-output")
                self.assertFalse((self.base / "limit-output").exists())

    def test_zip64_member_is_supported(self):
        archive = self.base / "zip64.zip"
        with zipfile.ZipFile(archive, "w", allowZip64=True) as zipped:
            with zipped.open("large-format-small-fixture.bin", "w", force_zip64=True) as handle:
                handle.write(b"synthetic ZIP64 payload")
        result = restore_archive(archive, self.base / "zip64-output")
        self.assertTrue(result["archive_crc_ok"])

    def test_sidecar_mismatch_is_refused_before_output(self):
        archive = self.archive()
        archive.with_suffix(".zip.sha256").write_text("0" * 64 + "  " + archive.name + "\n")
        with self.assertRaisesRegex(OfflineError, "sidecar"):
            restore_archive(archive, self.base / "bad-sidecar")
        self.assertFalse((self.base / "bad-sidecar").exists())

    def test_broken_symlink_sidecar_is_not_silently_ignored(self):
        archive = self.generic_zip([("payload.txt", b"content")])
        archive.with_suffix(".zip.sha256").symlink_to(self.base / "missing-sidecar")
        with self.assertRaises(OfflineError):
            restore_archive(archive, self.base / "broken-sidecar-output")

    def test_crc_damage_is_refused_and_source_stays_unchanged(self):
        archive = self.generic_zip([("payload.txt", b"unique-payload")])
        data = bytearray(archive.read_bytes())
        data[data.index(b"unique-payload")] ^= 1
        archive.write_bytes(data)
        before = sha256(archive)
        with self.assertRaisesRegex(OfflineError, "CRC"):
            restore_archive(archive, self.base / "bad-crc")
        self.assertEqual(before, sha256(archive))
        self.assertFalse((self.base / "bad-crc").exists())

    def test_corrupt_source_is_not_recertified_by_rebuild_or_vault(self):
        (self.obj / "content.ziw").write_bytes(b"damaged")
        self.assertFalse(inspect_archive(self.library)["ok"])
        for function in (rebuild_readable, export_vault):
            with self.assertRaisesRegex(OfflineError, "integrity"):
                function(self.library, self.base / function.__name__)

    def test_metadata_identity_mismatch_is_not_hidden_by_matching_file_sha(self):
        metadata = self.obj / "metadata.json"
        content = json.loads(metadata.read_text())
        content["docGuid"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        write_json(metadata, content)
        for entry in self.record["files"]:
            if entry["path"] == metadata.relative_to(self.library).as_posix():
                entry.update(size=metadata.stat().st_size, sha256=sha256(metadata))
        self.write_record()
        self.assertFalse(inspect_archive(self.library)["ok"])
        with self.assertRaises(OfflineError):
            rebuild_readable(self.library, self.base / "wrong-identity")

    def test_vault_omits_control_credentials_and_keeps_sibling_resource_layout(self):
        cache = self.library / "external-resources"
        cache.mkdir()
        asset = cache / ("a" * 64 + ".png")
        asset.write_bytes(b"synthetic cached image")
        write_json(cache / "manifest.json", {"not-a-real-token": "synthetic"})
        write_json(self.library / "metadata/user-cert.json", {"synthetic-private-certificate": True})
        write_json(self.library / ".private/auth.json", {"token": "synthetic-not-real"})
        (self.library / "index.html").write_text("<script>synthetic-unsafe-control</script>")
        self.record["files"].append({"path": asset.relative_to(self.library).as_posix(), "size": asset.stat().st_size, "sha256": sha256(asset)})
        self.write_record()
        result = export_vault(self.library, self.base / "portable-vault")
        target = self.base / "portable-vault"
        self.assertTrue((target / "readable").is_dir())
        self.assertTrue((target / "external-resources" / asset.name).is_file())
        self.assertFalse((target / "external-resources/manifest.json").exists())
        self.assertFalse((target / "metadata").exists())
        self.assertFalse((target / ".private").exists())
        self.assertFalse((target / "objects").exists())
        self.assertTrue(result["local_integrity_ok"])
        self.assertTrue((target / "readable/Empty").is_dir())
        self.assertNotIn("synthetic-unsafe-control", (target / "index.html").read_text())
        self.assertIn("Content-Security-Policy", (target / "index.html").read_text())

    def test_rebuild_reuses_only_registered_external_cache_without_network(self):
        url = "https://assets.example.invalid/synthetic.svg"
        archive = self.obj / "content.ziw"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("index.html", '<html><body><h1>Cached recovery</h1><img src="' + url + '"></body></html>')
        from wizbackup.exporter import export_note
        dest = self.library / self.record["readable_path"]
        exported = export_note(archive, dest, self.note)
        asset = self.library / "external-resources" / (hashlib.sha256(url.encode()).hexdigest() + ".svg")
        asset.parent.mkdir()
        asset.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="3" height="3"/>')
        local = os.path.relpath(asset, dest)
        for file in (dest / "index.html", dest / "note.md"):
            file.write_text(file.read_text().replace(url, local))
        exported.update(external_resources=[], warnings=[])
        metadata = json.loads((dest / "metadata.json").read_text())
        metadata["export"] = exported
        write_json(dest / "metadata.json", metadata)
        entry = {"status": "downloaded", "path": asset.relative_to(self.library).as_posix(),
                 "size": asset.stat().st_size, "sha256": sha256(asset), "role": "image"}
        write_json(asset.parent / "manifest.json", {"resources": {url: entry}})
        self.record["export"] = exported
        self.record["files"] = [{"path": p.relative_to(self.library).as_posix(), "size": p.stat().st_size, "sha256": sha256(p)}
                                for folder in (self.obj, dest) for p in sorted(folder.rglob("*")) if p.is_file()]
        self.record["files"].append({"path": entry["path"], "size": entry["size"], "sha256": entry["sha256"]})
        self.write_record()
        before = snapshot(self.library)
        with patch("requests.Session", side_effect=AssertionError("Network forbidden")):
            result = rebuild_readable(self.library, self.base / "cached-rebuilt")
        self.assertTrue(result["all_readable"])
        self.assertEqual(1, result["verified_external_assets_available"])
        rebuilt = self.base / "cached-rebuilt"
        page_path = next((rebuilt / "readable").rglob("index.html"))
        img = BeautifulSoup(page_path.read_text(), "html.parser").find("img")
        self.assertEqual((rebuilt / entry["path"]).resolve(), (page_path.parent / unquote(img["src"])).resolve())
        self.assertTrue((rebuilt / entry["path"]).is_file())
        self.assertEqual(before, snapshot(self.library))

    def test_cache_manifest_cannot_redirect_rebuild_to_original_or_certificate(self):
        cache = self.library / "external-resources"
        cache.mkdir()
        source = self.obj / "content.ziw"
        url = "https://assets.example.invalid/style.css"
        write_json(cache / "manifest.json", {"resources": {url: {
            "status": "downloaded", "path": source.relative_to(self.library).as_posix(),
            "sha256": sha256(source), "size": source.stat().st_size}}})
        before = snapshot(self.library)
        with self.assertRaisesRegex(OfflineError, "cache"):
            rebuild_readable(self.library, self.base / "unsafe-cache-output")
        self.assertEqual(before, snapshot(self.library))
        self.assertFalse((self.base / "unsafe-cache-output").exists())

    def test_symlink_source_assets_cannot_copy_outside_files(self):
        image = next((self.library / "readable").rglob("demo.svg"))
        outside = self.base / "outside.svg"
        outside.write_bytes(image.read_bytes())
        image.unlink()
        image.symlink_to(outside)
        with self.assertRaises(OfflineError):
            export_vault(self.library, self.base / "symlink-vault")

    def test_vault_ignores_known_os_metadata_and_reports_count_without_source_changes(self):
        folder = self.library / "readable/OS-metadata-only"
        folder.mkdir()
        for name in (".DS_Store", "Thumbs.db", "desktop.ini"):
            (folder / name).write_bytes(b"synthetic OS metadata")
        before = snapshot(self.library)
        destination = self.base / "vault-without-os-metadata"
        result = export_vault(self.library, destination)
        self.assertEqual(3, result["omitted_os_metadata"])
        for name in (".DS_Store", "Thumbs.db", "desktop.ini"):
            self.assertFalse(list(destination.rglob(name)))
        self.assertEqual(before, snapshot(self.library))

    def test_vault_still_refuses_unregistered_real_reading_files(self):
        (self.library / "readable/new-user-note.md").write_text("Synthetic unregistered user note")
        before = snapshot(self.library)
        destination = self.base / "must-not-publish-unregistered-note"
        with self.assertRaisesRegex(OfflineError, "registered"):
            export_vault(self.library, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(before, snapshot(self.library))

    def test_readability_warnings_remain_partial_after_restore(self):
        self.record["warnings"] = ["Source checksum metadata anomaly (synthetic)"]
        self.record["source_checksum_anomaly"] = {"synthetic": True}
        self.write_record()
        result = restore_archive(self.archive(), self.base / "partial-reading")
        self.assertTrue(result["local_integrity_ok"])
        self.assertFalse(result["all_readable"])
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["libraries"][0]["source_checksum_anomalies"])

    def encrypted_only(self):
        plaintext = (self.obj / "content.ziw").read_bytes()
        private = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        numbers = private.private_numbers()
        password = "synthetic-password"
        secret = b"synthetic ZIP encryption key"
        cert = {"n": str(numbers.public_numbers.n), "e": str(numbers.public_numbers.e),
                "d": base64.b64encode(aes_encrypt(str(numbers.d).encode(), password.encode())).decode()}
        wrapped = private.public_key().encrypt(secret, rsa_padding.PKCS1v15())
        encrypted = struct.pack("<4sII128s16s", b"ZIWR", 1, 128, wrapped, bytes(16)) + aes_encrypt(plaintext, secret)
        original = self.obj / "original.ziw"
        original.write_bytes(encrypted)
        (self.obj / "content.ziw").unlink()
        self.note["protected"] = 1
        write_json(self.library / "metadata/notes.json", [self.note])
        write_json(self.obj / "metadata.json", self.note)
        write_json(self.library / "metadata/user-cert.json", {"result": cert})
        self.record["files"] = [{"path": p.relative_to(self.library).as_posix(), "size": p.stat().st_size, "sha256": sha256(p)}
                                for folder in (self.obj, self.library / self.record["readable_path"])
                                for p in sorted(folder.rglob("*")) if p.is_file()]
        self.write_record()
        keyfile = self.base / "synthetic-password.txt"
        keyfile.write_text(password)
        keyfile.chmod(0o600)
        return original, keyfile

    def test_encrypted_inspection_and_offline_local_decryption(self):
        original, keyfile = self.encrypted_only()
        before = snapshot(self.library)
        info = inspect_archive(original)
        self.assertTrue(info["encrypted"])
        locked = rebuild_readable(self.library, self.base / "locked-reading")
        self.assertEqual({"locked": 1}, locked["statuses"])
        self.assertFalse(locked["all_readable"])
        decoded = rebuild_readable(self.library, self.base / "decoded-reading", password_file=keyfile)
        self.assertEqual({"exported": 1}, decoded["statuses"])
        self.assertTrue(decoded["all_readable"])
        self.assertFalse((self.base / "decoded-reading/metadata/user-cert.json").exists())
        self.assertEqual(before, snapshot(self.library))

    def test_missing_certificate_keeps_encrypted_note_locked_without_network(self):
        original, keyfile = self.encrypted_only()
        (self.library / "metadata/user-cert.json").unlink()
        result = rebuild_readable(self.library, self.base / "missing-cert", password_file=keyfile)
        self.assertEqual({"locked": 1}, result["statuses"])
        self.assertFalse(result["all_readable"])
        self.assertTrue(original.is_file())

    def test_large_encrypted_original_is_retained_without_unbounded_decryption(self):
        original, keyfile = self.encrypted_only()
        with patch("wizbackup.offline.MAX_ENCRYPTED_BYTES", 1), patch("wizbackup.offline.decrypt_ziw") as decrypt:
            result = rebuild_readable(self.library, self.base / "large-cipher", password_file=keyfile)
        self.assertEqual({"locked": 1}, result["statuses"])
        decrypt.assert_not_called()
        self.assertTrue(original.is_file())

    def test_inspection_does_not_echo_note_body_or_metadata_secrets(self):
        serialized = json.dumps(inspect_archive(self.library))
        self.assertNotIn("Synthetic personal note", serialized)
        self.assertNotIn("This demonstration", serialized)
        self.assertNotIn("token", serialized.lower())

    def test_empty_logical_categories_survive_restore_and_regeneration(self):
        restored = self.base / "with-empty-categories"
        restore_archive(self.archive(), restored)
        self.assertTrue((restored / self.library.name / "readable/Empty").is_dir())
        rebuilt = self.base / "rebuilt-empty-categories"
        rebuild_readable(self.library, rebuilt)
        self.assertTrue((rebuilt / "readable/Empty").is_dir())


if __name__ == "__main__":
    unittest.main()
