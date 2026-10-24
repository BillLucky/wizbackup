"""CLI acceptance boundaries, including offline operation without credentials."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from wizbackup.cli import main

class CLITests(unittest.TestCase):
    def test_plain_rebuild_does_not_infer_a_missing_default_password(self):
        create = runpy.run_path(str(Path(__file__).parents[1] / "examples/create_demo.py"))["create_demo"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); create(root / "demo", library_only=True)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["rebuild", "--source", str(root / "demo/personal"), "--destination", str(root / "rebuilt")])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out.getvalue())["local_integrity_ok"])
            self.assertFalse(json.loads(out.getvalue())["network_used"])

    def test_verify_core_damage_is_not_only_a_readability_warning(self):
        create = runpy.run_path(str(Path(__file__).parents[1] / "examples/create_demo.py"))["create_demo"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); create(root / "demo", library_only=True)
            library = root / "demo/personal"
            record = json.loads(next((library / "records").glob("*.json")).read_text())
            (library / record["files"][0]["path"]).write_bytes(b"damaged-synthetic-file")
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["verify", "--output", str(library)])
            self.assertEqual(code, 2)

    def test_explicit_missing_password_is_an_error(self):
        create = runpy.run_path(str(Path(__file__).parents[1] / "examples/create_demo.py"))["create_demo"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); create(root / "demo", library_only=True)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = main(["rebuild", "--source", str(root / "demo/personal"), "--destination", str(root / "rebuilt"), "--password-file", str(root / "missing-password")])
            self.assertEqual(code, 1)
