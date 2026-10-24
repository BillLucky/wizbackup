"""Offline reader for the documented WizQTClient ZIWR v1 envelope.

Protocol references: WizTeam/WizQTClient (commit 7067bcfc),
src/share/WizZiwReader.{h,cpp}, WizEnc.cpp, and WizMd5.cpp.
This module makes no network requests and never changes the encrypted source.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import os
from pathlib import Path
import re
import struct
import tempfile
import zipfile

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class DecryptionError(ValueError):
    """Invalid certificate, password, envelope, or decrypted archive."""


_HEADER = struct.Struct("<4sII128s16s")
_IV = b"0123456789abcdef"


def _integer(value: object) -> int:
    """Read positive Crypto++ Integer text: decimal, 0x hex, or hex-h."""
    if isinstance(value, bool):
        raise DecryptionError("Invalid certificate integer")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[0-9A-Fa-f]+[hH]", text):
            number = int(text[:-1], 16)
        elif re.fullmatch(r"0[xX][0-9A-Fa-f]+", text):
            number = int(text, 16)
        elif re.fullmatch(r"[0-9]+", text):
            number = int(text, 10)
        else:
            raise DecryptionError("Invalid certificate integer")
    else:
        raise DecryptionError("Invalid certificate integer")
    if number <= 0:
        raise DecryptionError("Invalid certificate integer")
    return number


def _aes_decrypt(data: bytes, password: bytes) -> bytes:
    if not password or b"\0" in password:
        raise DecryptionError("Invalid encryption password")
    if not data or len(data) % 16:
        raise DecryptionError("Truncated AES ciphertext")
    # Wiz uses the *hex text*, not the 16 raw digest bytes, as its AES key.
    key = hashlib.md5(password, usedforsecurity=False).hexdigest().encode("ascii")
    decryptor = Cipher(algorithms.AES(key), modes.CBC(_IV)).decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise DecryptionError("Incorrect password or damaged ciphertext") from None


def _private_key(cert: dict, password: str) -> rsa.RSAPrivateKey:
    try:
        n, e = _integer(cert["n"]), _integer(cert["e"])
        if n.bit_length() != 1024:
            raise DecryptionError("ZIWR v1 requires a 1024-bit RSA certificate")
        encoded_d = cert["d"]
        if not isinstance(encoded_d, str):
            raise DecryptionError("Invalid encrypted certificate")
        ciphertext = base64.b64decode("".join(encoded_d.split()), validate=True)
        d_text = _aes_decrypt(ciphertext, password.encode("utf-8")).decode("ascii")
        d = _integer(d_text)
        p, q = rsa.rsa_recover_prime_factors(n, e, d)
        return rsa.RSAPrivateNumbers(
            p=p,
            q=q,
            d=d,
            dmp1=rsa.rsa_crt_dmp1(d, p),
            dmq1=rsa.rsa_crt_dmq1(d, q),
            iqmp=rsa.rsa_crt_iqmp(p, q),
            public_numbers=rsa.RSAPublicNumbers(e=e, n=n),
        ).private_key()
    except DecryptionError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, binascii.Error):
        raise DecryptionError("Invalid certificate or certificate password") from None


def decrypt_ziw(raw: Path, dest: Path, cert: dict, password: str) -> None:
    """Decrypt a ZIWR v1 file to a verified ZIW/ZIP using a local certificate.

    ``cert`` contains ``n``, ``e`` and base64 encrypted ``d`` from the user's
    certificate. ``password`` is the separate certificate password. ZIP CRCs
    and the presence of index.html are checked before committing ``dest``.
    The envelope has no authenticated checksum: its reserved field is zero,
    not an MD5. Archive CRC validation detects many, but not all, alterations.
    """
    raw, dest = Path(raw), Path(dest)
    if raw.resolve() == dest.resolve():
        raise DecryptionError("Destination must differ from encrypted source")
    data = raw.read_bytes()
    if len(data) < _HEADER.size + 16:
        raise DecryptionError("Truncated ZIWR envelope")
    magic, version, key_length, wrapped_key, reserved = _HEADER.unpack_from(data)
    if magic != b"ZIWR":
        raise DecryptionError("Unsupported ZIW encryption type")
    if version != 1 or key_length != 128:
        raise DecryptionError("Unsupported ZIWR envelope version or key length")
    if reserved != bytes(16):
        raise DecryptionError("Invalid ZIWR reserved header field")
    private_key = _private_key(cert, password)
    try:
        ziw_password = private_key.decrypt(wrapped_key, rsa_padding.PKCS1v15())
    except ValueError:
        raise DecryptionError("Incorrect certificate or damaged RSA envelope") from None
    plaintext = _aes_decrypt(data[_HEADER.size :], ziw_password)
    try:
        with zipfile.ZipFile(io.BytesIO(plaintext)) as archive:
            if "index.html" not in archive.namelist():
                raise DecryptionError("Decrypted archive is missing index.html")
            if archive.testzip() is not None:
                raise DecryptionError("Decrypted archive failed CRC validation")
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
        raise DecryptionError("Decrypted data is not a valid ZIW archive") from None
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".decrypt-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(plaintext)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, dest)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
