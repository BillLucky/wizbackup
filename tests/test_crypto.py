"""Synthetic ZIWR fixtures; no account, password or cloud access required."""

import base64
import hashlib
import io
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wizbackup.crypto import DecryptionError, decrypt_ziw


def encrypt_fixture(data: bytes, password: bytes) -> bytes:
    key = hashlib.md5(password, usedforsecurity=False).hexdigest().encode("ascii")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(b"0123456789abcdef")).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


class ZiwCryptoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        cls.password = "合成证书密码-test"
        cls.secret = b"abcdef0123456789" * 4
        memory = io.BytesIO()
        with zipfile.ZipFile(memory, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("index.html", "<html><body>离线笔记</body></html>")
            archive.writestr("index_files/image.bin", bytes(range(256)))
        cls.plaintext = memory.getvalue()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.raw = Path(self.temp.name) / "original.ziw"
        self.dest = Path(self.temp.name) / "decrypted.ziw"

    def fixture(self, number_format="decimal", plaintext=None):
        numbers = self.private_key.private_numbers()
        fmt = (lambda value: str(value)) if number_format == "decimal" else (lambda value: f"{value:X}h")
        cert = {
            "n": fmt(numbers.public_numbers.n),
            "e": fmt(numbers.public_numbers.e),
            "d": base64.b64encode(encrypt_fixture(fmt(numbers.d).encode("ascii"), self.password.encode("utf-8"))).decode("ascii"),
        }
        wrapped = self.private_key.public_key().encrypt(self.secret, rsa_padding.PKCS1v15())
        header = struct.pack("<4sII128s16s", b"ZIWR", 1, 128, wrapped, bytes(16))
        self.assertEqual(156, len(header))
        self.raw.write_bytes(header + encrypt_fixture(self.plaintext if plaintext is None else plaintext, self.secret))
        return cert

    def test_decimal_certificate_and_full_archive_roundtrip(self):
        cert = self.fixture()
        original = self.raw.read_bytes()
        decrypt_ziw(self.raw, self.dest, cert, self.password)
        self.assertEqual(self.plaintext, self.dest.read_bytes())
        self.assertEqual(original, self.raw.read_bytes())
        self.assertEqual(0o600, self.dest.stat().st_mode & 0o777)

    def test_hex_h_certificate_and_encrypted_private_exponent(self):
        cert = self.fixture("hex")
        decrypt_ziw(self.raw, self.dest, cert, self.password)
        self.assertEqual(self.plaintext, self.dest.read_bytes())

    def test_wrong_password_leaves_existing_destination_untouched(self):
        cert = self.fixture()
        self.dest.write_bytes(b"previous-good-copy")
        with self.assertRaises(DecryptionError):
            decrypt_ziw(self.raw, self.dest, cert, "incorrect")
        self.assertEqual(b"previous-good-copy", self.dest.read_bytes())

    def test_truncated_header_and_body_are_rejected(self):
        cert = self.fixture()
        full = self.raw.read_bytes()
        for broken in (full[:155], full[:156], full[:-1]):
            with self.subTest(length=len(broken)):
                self.raw.write_bytes(broken)
                with self.assertRaises(DecryptionError):
                    decrypt_ziw(self.raw, self.dest, cert, self.password)
                self.assertFalse(self.dest.exists())

    def test_unknown_header_fields_are_rejected(self):
        cert = self.fixture()
        valid = self.raw.read_bytes()
        for offset, replacement in ((0, b"ZIWA"), (4, struct.pack("<I", 2)), (8, struct.pack("<I", 256)), (140, b"X")):
            with self.subTest(offset=offset):
                self.raw.write_bytes(valid[:offset] + replacement + valid[offset + len(replacement):])
                with self.assertRaises(DecryptionError):
                    decrypt_ziw(self.raw, self.dest, cert, self.password)

    def test_valid_padding_but_corrupt_zip_crc_is_rejected(self):
        changed = bytearray(self.plaintext)
        position = changed.index("离线笔记".encode("utf-8"))
        changed[position] ^= 1
        cert = self.fixture(plaintext=bytes(changed))
        with self.assertRaisesRegex(DecryptionError, "CRC"):
            decrypt_ziw(self.raw, self.dest, cert, self.password)
        self.assertFalse(self.dest.exists())

    def test_arbitrary_nonarchive_plaintext_is_rejected(self):
        cert = self.fixture(plaintext=b"not a ZIW archive")
        with self.assertRaises(DecryptionError):
            decrypt_ziw(self.raw, self.dest, cert, self.password)
        self.assertFalse(self.dest.exists())

    def test_source_cannot_be_overwritten(self):
        cert = self.fixture()
        original = self.raw.read_bytes()
        with self.assertRaises(DecryptionError):
            decrypt_ziw(self.raw, self.raw, cert, self.password)
        self.assertEqual(original, self.raw.read_bytes())


if __name__ == "__main__":
    unittest.main()
