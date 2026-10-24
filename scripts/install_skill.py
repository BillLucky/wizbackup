#!/usr/bin/env python3
"""Install only the bundled skill into a new agent skill directory.

The installer never changes an existing destination or agent configuration.
No credentials are read, no network request is made, and no tool data is copied.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat


class SkillInstallError(ValueError):
    """The skill cannot be installed without overwriting or unsafe copying."""


def bundled_skill() -> Path:
    return Path(__file__).resolve().parents[1] / "skills" / "wizbackup"


def default_destination() -> Path:
    return Path.home() / ".agents" / "skills" / "wizbackup"


def _bundle_files(source: Path) -> list[Path]:
    if source.is_symlink() or not source.is_dir():
        raise SkillInstallError("The bundled skill must be a real directory")
    entrypoint = source / "SKILL.md"
    if entrypoint.is_symlink() or not entrypoint.is_file():
        raise SkillInstallError("The bundled skill has no regular SKILL.md")
    files = []
    for current, directories, names in os.walk(source, followlinks=False):
        current = Path(current)
        for name in directories + names:
            item = current / name
            if item.is_symlink():
                raise SkillInstallError("Symbolic links are not permitted in the skill bundle")
            mode = item.stat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise SkillInstallError("Special files are not permitted in the skill bundle")
        files.extend(current / name for name in names)
    # Publish the discoverable entrypoint only after every supporting resource.
    return sorted(files, key=lambda p: (p == entrypoint, p.relative_to(source).as_posix()))


def install_skill(destination: Path | None = None, *, source: Path | None = None) -> dict:
    """Copy the instruction bundle only, refusing any existing target.

    destination is the full new skill-folder path, not its parent directory.
    A user-selected custom parent can resolve through a symlink, but the final
    destination itself must not be an existing file, directory, or symlink.
    """
    source = (Path(source) if source is not None else bundled_skill()).expanduser()
    if source.is_symlink():
        raise SkillInstallError("The bundled skill must not be a symbolic link")
    source = source.resolve()
    files = _bundle_files(source)
    selected = (Path(destination) if destination is not None else default_destination()).expanduser()
    if selected.is_symlink() or selected.exists():
        raise SkillInstallError("Destination already exists; no files were changed")
    parent = selected.parent.resolve()
    destination = parent / selected.name
    if destination.is_symlink() or destination.exists():
        raise SkillInstallError("Destination already exists; no files were changed")
    if source == destination or source in destination.parents:
        raise SkillInstallError("Destination must be outside the source skill bundle")
    parent.mkdir(parents=True, exist_ok=True)
    # mkdir is an atomic reservation. Even a newly appearing empty directory is
    # refused, rather than replaced by a rename or a merge.
    try:
        destination.mkdir(mode=0o700)
    except FileExistsError:
        raise SkillInstallError("Destination appeared during installation; it was not changed") from None
    installed = []
    try:
        for item in files:
            relative = item.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(item, read_flags), "rb") as inp:
                if not stat.S_ISREG(os.fstat(inp.fileno()).st_mode):
                    raise SkillInstallError("A source changed to a special file")
                with os.fdopen(os.open(target, write_flags, 0o600), "wb") as out:
                    while chunk := inp.read(1024 * 1024):
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
            installed.append(relative.as_posix())
    except (OSError, SkillInstallError):
        # Existing user data is never deleted, and even this new partial
        # directory is retained for review. SKILL.md is copied last.
        raise SkillInstallError("Installation did not complete; the new destination was retained for review") from None
    return {"ok": True, "skill": "wizbackup", "destination": str(destination), "files_installed": installed,
            "invoke": "$wizbackup", "configuration_changed": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Install the WizBackup skill without overwriting agent files")
    parser.add_argument("--destination", type=Path,
                        help="Full new skill directory (default: ~/.agents/skills/wizbackup)")
    args = parser.parse_args(argv)
    try:
        result = install_skill(args.destination)
    except (SkillInstallError, OSError) as exc:
        parser.exit(1, "Skill installation refused: " + str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
