"""Skill distribution tests with synthetic trees; no real agent config is touched."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "install_skill.py"
_SPEC = importlib.util.spec_from_file_location("wizbackup_skill_install", _SCRIPT)
_INSTALL = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_INSTALL)


def snapshot(root: Path):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


class SkillInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "checkout" / "skills" / "wizbackup"
        (self.source / "references").mkdir(parents=True)
        (self.source / "agents").mkdir()
        (self.source / "SKILL.md").write_text(
            "---\nname: wizbackup\ndescription: Synthetic installation fixture.\n---\n"
            "Read [workflow](references/workflow.md).\n")
        (self.source / "references/workflow.md").write_text("Synthetic public instructions.\n")
        (self.source / "agents/openai.yaml").write_text('policy:\n  allow_implicit_invocation: true\n')
        private = self.root / "checkout/private"
        private.mkdir()
        (private / "auth.json").write_text('{"token":"SYNTHETIC_DO_NOT_COPY"}')
        self.target = self.root / "agent" / "skills" / "wizbackup"

    def test_install_copies_only_skill_bundle_and_keeps_source_unchanged(self):
        before = snapshot(self.root / "checkout")
        result = _INSTALL.install_skill(self.target, source=self.source)
        self.assertTrue(result["ok"])
        self.assertEqual(snapshot(self.source), snapshot(self.target))
        self.assertFalse((self.target / "private").exists())
        self.assertNotIn("SYNTHETIC_DO_NOT_COPY", "\n".join(p.read_text() for p in self.target.rglob("*") if p.is_file()))
        self.assertEqual(before, snapshot(self.root / "checkout"))

    def test_default_destination_uses_user_agents_directory(self):
        home = self.root / "synthetic-home"
        with patch.object(_INSTALL.Path, "home", return_value=home):
            result = _INSTALL.install_skill(source=self.source)
        self.assertEqual(Path(result["destination"]), home / ".agents/skills/wizbackup")
        self.assertFalse((home / ".claude").exists())
        self.assertFalse((home / ".codex").exists())

    def test_existing_empty_directory_is_not_merged_or_replaced(self):
        self.target.mkdir(parents=True)
        with self.assertRaises(_INSTALL.SkillInstallError):
            _INSTALL.install_skill(self.target, source=self.source)
        self.assertTrue(self.target.is_dir())
        self.assertEqual([], list(self.target.iterdir()))

    def test_existing_files_and_configuration_remain_unchanged(self):
        self.target.mkdir(parents=True)
        (self.target / "SKILL.md").write_text("User's existing skill")
        before = snapshot(self.target)
        with self.assertRaises(_INSTALL.SkillInstallError):
            _INSTALL.install_skill(self.target, source=self.source)
        self.assertEqual(before, snapshot(self.target))

    def test_existing_file_destination_is_not_changed(self):
        self.target.parent.mkdir(parents=True)
        self.target.write_text("User file")
        with self.assertRaises(_INSTALL.SkillInstallError):
            _INSTALL.install_skill(self.target, source=self.source)
        self.assertEqual("User file", self.target.read_text())

    def test_destination_symlinks_are_rejected_even_when_dangling(self):
        self.target.parent.mkdir(parents=True)
        for exists in (False, True):
            with self.subTest(existing_target=exists):
                link = self.target.parent / ("link-" + str(exists))
                victim = self.root / ("victim-" + str(exists))
                if exists:
                    victim.mkdir()
                    (victim / "keep.txt").write_text("keep")
                link.symlink_to(victim, target_is_directory=True)
                with self.assertRaises(_INSTALL.SkillInstallError):
                    _INSTALL.install_skill(link, source=self.source)
                self.assertTrue(link.is_symlink())
                if exists:
                    self.assertEqual("keep", (victim / "keep.txt").read_text())
                else:
                    self.assertFalse(victim.exists())

    def test_source_links_are_refused_before_destination_creation(self):
        (self.source / "outside-link").symlink_to(self.root / "checkout/private/auth.json")
        with self.assertRaises(_INSTALL.SkillInstallError):
            _INSTALL.install_skill(self.target, source=self.source)
        self.assertFalse(self.target.exists())

    def test_user_selected_new_agent_directory_is_supported(self):
        target = self.root / "another-agent" / ".claude" / "skills" / "wizbackup"
        result = _INSTALL.install_skill(target, source=self.source)
        self.assertTrue(result["ok"])
        self.assertEqual(snapshot(self.source), snapshot(target))

    def test_installing_inside_source_is_rejected_without_changing_source(self):
        before = snapshot(self.source)
        with self.assertRaises(_INSTALL.SkillInstallError):
            _INSTALL.install_skill(self.source / "nested", source=self.source)
        self.assertEqual(before, snapshot(self.source))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires a Unix filesystem")
    def test_source_special_files_are_refused_without_opening_them(self):
        os.mkfifo(self.source / "not-a-file")
        with self.assertRaises(_INSTALL.SkillInstallError):
            _INSTALL.install_skill(self.target, source=self.source)
        self.assertFalse(self.target.exists())

    def test_relative_source_api_resolves_before_copying(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            _INSTALL.install_skill(self.target, source=self.source.relative_to(self.root))
        finally:
            os.chdir(previous)
        self.assertEqual(snapshot(self.source), snapshot(self.target))

    def test_failed_copy_does_not_publish_a_discoverable_entrypoint(self):
        original_open = os.open
        def failing_open(path, flags, *args, **kwargs):
            if Path(path) == self.target / "references/workflow.md":
                raise OSError("Synthetic write failure")
            return original_open(path, flags, *args, **kwargs)
        with patch.object(_INSTALL.os, "open", side_effect=failing_open):
            with self.assertRaises(_INSTALL.SkillInstallError):
                _INSTALL.install_skill(self.target, source=self.source)
        self.assertTrue(self.target.exists())
        self.assertFalse((self.target / "SKILL.md").exists())
        self.assertTrue((self.source / "SKILL.md").exists())

    def test_cli_works_from_an_unrelated_directory_and_installed_references_resolve(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        result = subprocess.run([sys.executable, str(_SCRIPT), "--destination", str(self.target)],
                                cwd=elsewhere, check=True, capture_output=True, text=True)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertTrue((self.target / "agents/openai.yaml").is_file())
        for document in self.target.rglob("*.md"):
            for linked in re.findall(r"\[[^\]]*\]\(([^)]+)\)", document.read_text()):
                if "://" in linked or linked.startswith("#"):
                    continue
                target = (document.parent / linked.split("#", 1)[0]).resolve()
                self.assertTrue(target.is_relative_to(self.target))
                self.assertTrue(target.is_file())
        denied = subprocess.run([sys.executable, str(_SCRIPT), "--destination", str(self.target)],
                                cwd=elsewhere, capture_output=True, text=True)
        self.assertNotEqual(0, denied.returncode)
        self.assertIn("refused", denied.stderr.lower())


if __name__ == "__main__":
    unittest.main()
