import hashlib
import io
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from wizbackup.attachments import legacy_md5, materialize_attachment
from wizbackup.client import ApiError


def zip_bytes(data=b"user attachment", name="original.dat", *, symlink=False, extra=False):
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_STORED) as package:
        info = zipfile.ZipInfo(name)
        if symlink:
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.writestr(info, data)
        if extra:
            package.writestr("another.dat", b"another")
    return memory.getvalue()


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = Path(self.temp.name) / "raw.bin"
        self.dest = Path(self.temp.name) / "restored.dat"

    def metadata(self, data, *, legacy=False, size=None):
        digest = hashlib.md5(data).digest()
        return {"dataMd5": legacy_md5(digest) if legacy else digest.hex(),
                "dataSize": len(data) if size is None else size}

    def test_known_legacy_digest_uses_four_little_endian_dwords(self):
        digest = bytes.fromhex("0123456789abcdeffedcba9876543210")
        self.assertEqual("67452301efcdab8998badcfe10325476", legacy_md5(digest))

    def test_real_user_zip_matching_raw_md5_is_not_unwrapped(self):
        content = zip_bytes()
        self.raw.write_bytes(content)
        result = materialize_attachment(self.raw, self.metadata(content), self.dest)
        self.assertEqual("raw-md5", result["validation"])
        self.assertEqual(content, self.dest.read_bytes())
        self.assertTrue(zipfile.is_zipfile(self.dest))
        self.assertEqual(content, self.raw.read_bytes())

    def test_raw_legacy_md5_matches_without_unwrap(self):
        content = zip_bytes()
        self.raw.write_bytes(content)
        result = materialize_attachment(self.raw, self.metadata(content, legacy=True), self.dest)
        self.assertEqual("raw-legacy-md5", result["validation"])
        self.assertEqual(content, self.dest.read_bytes())

    def test_single_member_standard_md5_with_transport_size(self):
        original = b"original-payload"
        wrapped = zip_bytes(original)
        self.raw.write_bytes(wrapped)
        result = materialize_attachment(self.raw, self.metadata(original, size=len(wrapped)), self.dest)
        self.assertEqual("wrapped-member-md5", result["validation"])
        self.assertEqual("raw-size", result["size_validation"])
        self.assertEqual(original, self.dest.read_bytes())
        self.assertEqual(wrapped, self.raw.read_bytes())

    def test_single_member_legacy_md5_with_original_size(self):
        original = b"legacy-payload"
        self.raw.write_bytes(zip_bytes(original, name="old-name.dat"))
        result = materialize_attachment(self.raw, self.metadata(original, legacy=True), self.dest)
        self.assertEqual("wrapped-member-legacy-md5", result["validation"])
        self.assertEqual("member-size", result["size_validation"])
        self.assertEqual(original, self.dest.read_bytes())
        self.assertEqual(hashlib.sha256(original).hexdigest(), result["sha256"])

    def test_verified_wrapper_checksum_with_stale_size_reports_anomaly(self):
        original = b"payload"
        self.raw.write_bytes(zip_bytes(original))
        self.dest.write_bytes(b"previous-copy")
        result = materialize_attachment(self.raw, self.metadata(original, size=123456), self.dest)
        self.assertEqual(original, self.dest.read_bytes())
        self.assertEqual("wrapped-member-md5", result["validation"])
        self.assertEqual("checksum-verified-size-anomaly", result["size_validation"])
        self.assertFalse(result["metadata_size_matches"])
        self.assertEqual(123456, result["metadata_size"])
        self.assertEqual(len(original), result["materialized_size"])

    def test_verified_raw_checksum_with_stale_zero_size_reports_anomaly(self):
        original = b"raw-payload"
        self.raw.write_bytes(original)
        result = materialize_attachment(self.raw, self.metadata(original, size=0), self.dest)
        self.assertEqual(original, self.dest.read_bytes())
        self.assertEqual("raw-md5", result["validation"])
        self.assertEqual("checksum-verified-size-anomaly", result["size_validation"])
        self.assertFalse(result["metadata_size_matches"])
        self.assertEqual(0, result["metadata_size"])
        self.assertEqual(len(original), result["raw_size"])

    def test_wrong_checksum_preserves_existing_destination(self):
        original = b"payload"
        self.raw.write_bytes(zip_bytes(original))
        self.dest.write_bytes(b"previous-copy")
        with self.assertRaises(ApiError):
            materialize_attachment(self.raw, self.metadata(b"different", size=123456), self.dest)
        self.assertEqual(b"previous-copy", self.dest.read_bytes())

    def test_multi_member_zip_does_not_satisfy_member_checksum(self):
        original = b"payload"
        self.raw.write_bytes(zip_bytes(original, extra=True))
        with self.assertRaises(ApiError):
            materialize_attachment(self.raw, self.metadata(original), self.dest)
        self.assertFalse(self.dest.exists())

    def test_corrupt_crc_rejected_even_if_member_md5_could_match(self):
        original = b"payload-for-crc"
        damaged = bytearray(zip_bytes(original))
        position = damaged.index(original)
        damaged[position] ^= 1
        self.raw.write_bytes(damaged)
        with self.assertRaises(ApiError):
            materialize_attachment(self.raw, self.metadata(original, size=len(damaged)), self.dest)
        self.assertFalse(self.dest.exists())

    def test_symlink_and_parent_path_members_rejected(self):
        original = b"payload"
        for name, symlink in (("link", True), ("../escape", False), ("/escape", False)):
            with self.subTest(name=name):
                wrapped = zip_bytes(original, name=name, symlink=symlink)
                self.raw.write_bytes(wrapped)
                with self.assertRaises(ApiError):
                    materialize_attachment(self.raw, self.metadata(original, size=len(wrapped)), self.dest)
                self.assertFalse(self.dest.exists())

    def test_size_limit_is_enforced_before_extracting(self):
        original = b"oversized-fixture"
        self.raw.write_bytes(zip_bytes(original))
        with patch("wizbackup.attachments.MAX_MEMBER_SIZE", 4):
            with self.assertRaises(ApiError):
                materialize_attachment(self.raw, self.metadata(original), self.dest)

    def test_wrong_checksum_and_source_overwrite_are_rejected(self):
        self.raw.write_bytes(b"payload")
        with self.assertRaises(ApiError):
            materialize_attachment(self.raw, self.metadata(b"other"), self.dest)
        with self.assertRaises(ApiError):
            materialize_attachment(self.raw, self.metadata(b"payload"), self.raw)
        self.assertEqual(b"payload", self.raw.read_bytes())


if __name__ == "__main__":
    unittest.main()
