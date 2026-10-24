import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("publication_audit", Path(__file__).parents[1] / "scripts/publication_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

class PublicationAuditTests(unittest.TestCase):
    def test_private_value_is_reported_without_echoing_it(self):
        secret = b"synthetic-but-private-value"
        findings = audit.scan_bytes("fixture.txt", b"data:" + secret, [secret])
        self.assertEqual(findings, [{"file": "fixture.txt", "reason": "private-value-match"}])
        self.assertNotIn(secret.decode(), str(findings))

    def test_generated_credentials_and_runtime_files_are_rejected(self):
        for name in [".private/auth.json", "backups/library/metadata.json", "notes.ziw", "client.sqlite3", ".env"]:
            self.assertIsNotNone(audit.path_problem(name))
        self.assertIsNone(audit.path_problem("examples/create_demo.py"))
        self.assertIsNone(audit.path_problem("skills/wizbackup/SKILL.md"))

    def test_literal_service_credentials_are_detected(self):
        credential = b"ghp_" + b"A" * 36
        self.assertTrue(audit.scan_bytes("fixture.txt", credential))
        self.assertFalse(audit.scan_bytes("fixture.txt", b'{"token": "YOUR_SYNTHETIC_SESSION_TOKEN_EXAMPLE"}'))
