"""Verify modern and legacy Wiz attachments without changing raw objects.

Legacy clients sometimes hash the original file with Wiz's DWORD-formatted MD5
while transporting a single-file ZIP. Raw checksum matches always take priority
so a real user ZIP remains a ZIP. No archive member path is used as an output
path; restoration writes exclusively to the caller-provided destination.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import zipfile

from .client import ApiError


MAX_MEMBER_SIZE = 4 * 1024**3
MAX_COMPRESSION_RATIO = 10_000
_BLOCK_SIZE = 1024 * 1024


def legacy_md5(digest: bytes) -> str:
    """WizMd5StringNoSpace: print four little-endian DWORDs as %08x."""
    return b"".join(digest[index:index + 4][::-1] for index in range(0, 16, 4)).hex()


def _digest_file(path):
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(_BLOCK_SIZE), b""):
            md5.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    return size, md5.digest(), sha256.hexdigest()


def _check_zip_member(info):
    name = info.filename
    path = PurePosixPath(name)
    if (not name or name.startswith(("/", "\\")) or "\\" in name
            or re.match(r"^[a-zA-Z]:", name)
            or any(part in (".", "..") for part in name.rstrip("/").split("/"))
            or any(ord(char) < 32 for char in name)):
        raise ApiError("Unsafe attachment transport member path")
    mode = (info.external_attr >> 16) & 0xFFFF
    if stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
        raise ApiError("Attachment transport contains a link or special file")
    if info.flag_bits & 1:
        raise ApiError("Attachment transport ZIP is password encrypted")
    if info.file_size > MAX_MEMBER_SIZE:
        raise ApiError("Attachment transport member exceeds the extraction size limit")
    if info.file_size > _BLOCK_SIZE and info.file_size / max(1, info.compress_size) > MAX_COMPRESSION_RATIO:
        raise ApiError("Attachment transport compression ratio exceeds the extraction limit")


def materialize_attachment(raw: Path, metadata: dict, dest: Path) -> dict:
    """Validate an attachment and atomically restore its usable file to dest.

    Accept standard MD5 or the verified legacy DWORD representation of either
    the entire raw object or its sole regular ZIP member. Only a checksum match
    on the member identifies a transport wrapper. Size must agree with the raw
    transfer or that verified member when size metadata is current. A checksum
    match with stale size metadata is recorded as an explicit anomaly. On any
    error, raw and existing dest stay unchanged. The returned SHA-256 values
    identify both forms independently.
    """
    raw, dest = Path(raw), Path(dest)
    if raw.resolve() == dest.resolve():
        raise ApiError("Attachment destination must differ from the raw object")
    expected_md5 = metadata.get("dataMd5")
    if not isinstance(expected_md5, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", expected_md5):
        raise ApiError("Attachment metadata lacks a valid MD5 checksum")
    expected_md5 = expected_md5.lower()
    try:
        expected_size = int(metadata["dataSize"])
        if (expected_size < 0 or isinstance(metadata["dataSize"], bool)
                or isinstance(metadata["dataSize"], float) and metadata["dataSize"] != expected_size):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ApiError("Attachment metadata lacks a valid byte size") from None
    raw_size, raw_digest, raw_sha256 = _digest_file(raw)
    raw_mode = ("raw-md5" if raw_digest.hex() == expected_md5 else
                "raw-legacy-md5" if legacy_md5(raw_digest) == expected_md5 else None)
    if not raw_mode and not zipfile.is_zipfile(raw):
        raise ApiError("Attachment MD5 mismatch; no recognized transport wrapper")
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=".attachment-", dir=dest.parent)
    temporary = Path(temporary_name)
    materialized_md5 = hashlib.md5(usedforsecurity=False)
    materialized_sha256 = hashlib.sha256()
    size = 0
    archive = None
    try:
        with os.fdopen(fd, "wb") as output:
            if raw_mode:
                source = raw.open("rb")
                validation = raw_mode
                size_validation = ("raw-size" if raw_size == expected_size else
                                   "checksum-verified-size-anomaly")
            else:
                archive = zipfile.ZipFile(raw)
                entries = archive.infolist()
                if len(entries) > 1000:
                    raise ApiError("Attachment transport has too many entries")
                for info in entries:
                    _check_zip_member(info)
                members = [info for info in entries if not info.is_dir()]
                if len(members) != 1:
                    raise ApiError("Attachment MD5 mismatch; ZIP is not a single-file transport")
                member = members[0]
                source = archive.open(member)
                validation = None
                size_validation = ("raw-size" if expected_size == raw_size else
                                   "member-size" if expected_size == member.file_size else
                                   "checksum-verified-size-anomaly")
            with source:
                for chunk in iter(lambda: source.read(_BLOCK_SIZE), b""):
                    size += len(chunk)
                    if not raw_mode and (size > MAX_MEMBER_SIZE or size > member.file_size):
                        raise ApiError("Attachment transport exceeds its declared extraction size")
                    output.write(chunk)
                    materialized_md5.update(chunk)
                    materialized_sha256.update(chunk)
            if raw_mode:
                if size != raw_size or materialized_sha256.hexdigest() != raw_sha256:
                    raise ApiError("Raw attachment changed during restoration")
            else:
                digest = materialized_md5.digest()
                if size != member.file_size:
                    raise ApiError("Attachment transport member size mismatch")
                if digest.hex() == expected_md5:
                    validation = "wrapped-member-md5"
                elif legacy_md5(digest) == expected_md5:
                    validation = "wrapped-member-legacy-md5"
                else:
                    raise ApiError("Attachment MD5 mismatch in raw object and ZIP member")
                # Reading to EOF above verifies the ZIP CRC. Verify the raw
                # file itself did not change between hashing and extraction.
                if _digest_file(raw)[2] != raw_sha256:
                    raise ApiError("Raw attachment changed during restoration")
            output.flush()
            os.fsync(output.fileno())
        if archive is not None:
            archive.close()
            archive = None
        os.replace(temporary, dest)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
        raise ApiError("Attachment transport failed ZIP/CRC validation") from None
    finally:
        if archive is not None:
            archive.close()
        if temporary.exists():
            temporary.unlink()
    return {
        "validation": validation, "size_validation": size_validation,
        "raw_size": raw_size, "materialized_size": size,
        "raw_sha256": raw_sha256, "sha256": materialized_sha256.hexdigest(),
        "raw_md5": raw_digest.hex(), "materialized_md5": materialized_md5.hexdigest(),
        "metadata_md5": expected_md5, "metadata_size": expected_size,
        "metadata_size_matches": size_validation != "checksum-verified-size-anomaly",
    }
