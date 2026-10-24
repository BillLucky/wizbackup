import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from wizbackup.history import archive_history


class Response:
    def __init__(self, data=None, status=200):
        self.data = data
        self.status_code = status
        self.is_redirect = False

    def json(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Session:
    def __init__(self, *, outage=False, locked=False):
        self.calls = []
        self.outage, self.locked = outage, locked

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.outage:
            return Response(status=500)
        if self.locked:
            return Response({"returnCode": 200, "noteData": {"url": "https://test.wiz.cn/locked.ziw"}})
        number = kwargs["params"]["version"]
        return Response({"returnCode": 200, "noteData": {
            "html": f'<html><body><p>Version {number}</p><img src="index_files/image.png"></body></html>',
            "resources": [{"name": "image.png", "url": "https://test.wiz.cn/image.png", "size": 6}],
        }})


class Client:
    server = "https://test.wiz.cn"
    kb = "11111111-1111-1111-1111-111111111111"
    token = "synthetic-history-secret"
    retries = 0
    timeout = 10

    def __init__(self, **kwargs):
        self.session = Session(**kwargs)
        self.downloads = []

    def download(self, url, target, **kwargs):
        self.downloads.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"ZIWR" + bytes(160) if url.endswith("locked.ziw") else b"binary")


def inventory(count):
    return {"returnCode": 200, "result": {"objType": "document", "versionInfos": [
        {"version": n + 1, "editorGuid": "editor", "encryption": 0} for n in range(count)
    ]}}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name)
        self.note = {"docGuid": "22222222-2222-2222-2222-222222222222", "title": "Synthetic History"}

    def test_all_versions_html_resources_and_hash_verified_resume(self):
        client = Client()
        report = archive_history(client, self.note, inventory(4), self.dest)
        self.assertEqual("archived", report["status"])
        self.assertEqual(4, report["downloaded"])
        self.assertEqual(4, len(client.session.calls))
        self.assertEqual(4, len(client.downloads))
        for url, kwargs in client.session.calls:
            self.assertIn("/ks/history/view/", url)
            self.assertFalse(kwargs["allow_redirects"])
        again = archive_history(client, self.note, inventory(4), self.dest)
        self.assertEqual(4, again["downloaded"])
        self.assertEqual(4, len(client.session.calls))
        original = next(self.dest.glob("versions/*/original.html"))
        original.write_text("damaged")
        archive_history(client, self.note, inventory(4), self.dest)
        self.assertEqual(5, len(client.session.calls))
        self.assertNotIn(b"synthetic-history-secret", (self.dest / "history-report.json").read_bytes())

    def test_outage_circuit_breaker_preserves_pending_and_retries_later(self):
        client = Client(outage=True)
        report = archive_history(client, self.note, inventory(8), self.dest)
        self.assertEqual("partial", report["status"])
        self.assertEqual(0, report["downloaded"])
        self.assertEqual(3, report["failed"])
        self.assertEqual(5, report["pending"])
        self.assertEqual(3, len(client.session.calls))
        self.assertEqual(8, len(list(self.dest.glob("versions/*/version.json"))))
        client.session.outage = False
        repaired = archive_history(client, self.note, inventory(8), self.dest)
        self.assertEqual("archived", repaired["status"])
        self.assertEqual(8, repaired["downloaded"])

    def test_encrypted_original_counts_as_downloaded_but_locked(self):
        report = archive_history(Client(locked=True), self.note, inventory(1), self.dest)
        self.assertEqual(1, report["locked"])
        self.assertEqual(1, report["downloaded"])
        self.assertEqual("archived", report["status"])

    def test_inventory_continuation_cannot_silently_count_as_complete(self):
        data = inventory(1)
        data["result"].update({"hasMore": True, "totalCount": 100})
        report = archive_history(Client(), self.note, data, self.dest)
        self.assertEqual("partial", report["status"])
        self.assertTrue(report["warnings"])

    def test_nested_result_note_data_and_version_identity(self):
        client = Client()
        original = client.session.get
        def nested(url, **kwargs):
            value = original(url, **kwargs)
            value.data = {"returnCode": 200, "result": {"noteData": value.data["noteData"]}}
            return value
        client.session.get = nested
        data = inventory(2)
        data["result"]["versionInfos"][1]["version"] = 1
        data["result"]["versionInfos"][1]["editorGuid"] = "another-editor"
        report = archive_history(client, self.note, data, self.dest)
        self.assertEqual(2, report["downloaded"])
        self.assertEqual(2, len(list(self.dest.glob("versions/*/content.ziw"))))


if __name__ == "__main__":
    unittest.main()
