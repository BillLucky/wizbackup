import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from bs4 import BeautifulSoup

from wizbackup.client import ApiError
from wizbackup.collaboration import archive_collaboration, _request


class Response:
    def __init__(self, value=None, *, content=None, status=200, headers=None):
        self.value = value
        self.content = content if content is not None else json.dumps(value).encode()
        self.status_code = status
        self.headers = headers or {"Content-Type": "application/octet-stream"}
        self.is_redirect = status in (301, 302, 303, 307, 308)

    def json(self):
        return self.value if self.value is not None else json.loads(self.content)

    def close(self):
        pass

    def iter_content(self, size):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Session:
    def __init__(self, resources_fail=False, empty_snapshot=False, html=None):
        self.calls = []
        self.resources_fail = resources_fail
        self.empty_snapshot = empty_snapshot
        self.html = html

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/tokens"):
            return Response({"returnCode": 200, "result": {"editorToken": "synthetic-editor-secret"}})
        if url.endswith("/versions/latest"):
            return Response({"data": "{}" if self.empty_snapshot else json.dumps({"blocks": [{"type": "text", "text": [{"insert": "Offline example"}]}]}), "mtime": 123})
        if url.endswith("/export/html"):
            return Response(content=self.html if self.html is not None else b'<html><body><p>Offline example</p><img src="image.png"><script>blocked()</script></body></html>')
        if url.endswith("/resources"):
            return Response(["image.png", "unreferenced.bin"])
        if "/resources/" in url:
            return Response(content=b"synthetic-binary", status=404 if self.resources_fail else 200)
        raise AssertionError("Unexpected endpoint")


class Client:
    server = "https://test.wiz.cn"
    kb = "11111111-1111-1111-1111-111111111111"
    token = "synthetic-account-secret"
    timeout = 10
    retries = 0

    def __init__(self, **kwargs):
        self.session = Session(**kwargs)


class CollaborationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dest = Path(self.temp.name)
        self.note = {"docGuid": "22222222-2222-2222-2222-222222222222", "title": "Synthetic", "category": "/test/"}

    def test_rest_archives_snapshot_and_all_resources_without_document_writes(self):
        client = Client()
        result = archive_collaboration(client, self.note, self.dest)
        self.assertEqual("archived", result["status"])
        self.assertEqual(2, result["resources"])
        snapshot = json.loads((self.dest / "snapshot.json").read_text())
        self.assertEqual("text", snapshot["blocks"][0]["type"])
        with zipfile.ZipFile(result["archive_path"]) as package:
            names = package.namelist()
            self.assertEqual(2, sum(n.startswith("index_files/") for n in names))
            self.assertIn(b"index_files/image--", package.read("index.html"))
        for method, url, kwargs in client.session.calls:
            if method == "POST":
                self.assertTrue(url.endswith("/tokens"))
                self.assertNotIn("json", kwargs)
                self.assertNotIn("data", kwargs)
            else:
                self.assertEqual("GET", method)
        export_call = next(kwargs for _, url, kwargs in client.session.calls if url.endswith("/export/html"))
        self.assertEqual("false", export_call["params"]["os"])
        # Authentication material is never in any persisted archival artifact.
        for path in self.dest.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"synthetic-editor-secret", path.read_bytes())
                self.assertNotIn(b"synthetic-account-secret", path.read_bytes())

    def test_resource_errors_are_explicit_partial_backup(self):
        result = archive_collaboration(Client(resources_fail=True), self.note, self.dest)
        self.assertEqual("partial", result["status"])
        self.assertEqual(2, len(result["failed_resources"]))
        self.assertEqual(0, result["resources"])
        self.assertTrue((self.dest / "snapshot.json").is_file())

    def test_missing_snapshot_cannot_be_reported_as_archived(self):
        with self.assertRaises(ApiError):
            archive_collaboration(Client(empty_snapshot=True), self.note, self.dest)
        self.assertFalse((self.dest / "content.ziw").exists())

    def test_redirect_to_oss_strips_all_editor_credentials(self):
        client = Client()
        responses = [Response(status=302, headers={"Location": "https://bucket.oss-cn-test.aliyuncs.com/object?signature=synthetic"}), Response(content=b"binary")]
        with patch.object(client.session, "request", side_effect=responses) as request:
            response = _request(client, "GET", client.server + "/editor/resource", headers={"x-live-editor-token": "private"})
        self.assertEqual(b"binary", response.content)
        self.assertEqual({}, request.call_args_list[1].kwargs["headers"])

    def test_token_post_cannot_follow_redirect(self):
        client = Client()
        with patch.object(client.session, "request", return_value=Response(status=302, headers={"Location": "https://evil.example/"})) as request:
            with self.assertRaises(ApiError):
                _request(client, "POST", client.server + "/tokens", headers={"x-wiz-token": "private"})
        self.assertEqual(1, request.call_count)

    def test_fullwidth_colon_hyperlink_does_not_abort_document(self):
        bad = "https://example.test\uff1a8080/path"
        original = ('<html><body><a href="' + bad + '">Keep this anchor text</a><img src="image.png"></body></html>').encode()
        result = archive_collaboration(Client(html=original), self.note, self.dest)
        self.assertEqual(result["status"], "archived")
        self.assertEqual(result["malformed_links"], 1)
        self.assertEqual(result["missing_html_resources"], [])
        self.assertEqual((self.dest / "server-export.html").read_bytes(), original)
        with zipfile.ZipFile(result["archive_path"]) as package:
            soup = BeautifulSoup(package.read("index.html"), "html.parser")
            self.assertEqual(soup.a["href"], "#invalid-reference")
            self.assertEqual(soup.a.get_text(), "Keep this anchor text")
            self.assertIn("index_files/", soup.img["src"])
        manifest = (self.dest / "resource-manifest.json").read_text()
        self.assertNotIn(bad, manifest)

    def test_malformed_ipv6_hyperlink_does_not_abort_document(self):
        original = b'<html><body><a href="https://[broken-ipv6/path">Preserved text</a></body></html>'
        result = archive_collaboration(Client(html=original), self.note, self.dest)
        self.assertEqual(result["status"], "archived")
        self.assertEqual(result["malformed_links"], 1)
        self.assertEqual((self.dest / "server-export.html").read_bytes(), original)
        readable = BeautifulSoup((self.dest / "readable/index.html").read_text(), "html.parser")
        self.assertEqual(readable.a["href"], "#invalid-reference")
        self.assertEqual(readable.a.get_text(), "Preserved text")

    def test_malformed_image_url_is_explicit_partial_not_hidden_success(self):
        bad = "https://example.test\uff1a8080/image.png"
        original = ('<html><body><p>Keep body text</p><img src="' + bad + '"></body></html>').encode()
        result = archive_collaboration(Client(html=original), self.note, self.dest)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["malformed_links"], 0)
        self.assertTrue(result["missing_html_resources"])
        self.assertTrue(result["missing_html_resources"][0].startswith("malformed-src-"))
        self.assertEqual((self.dest / "server-export.html").read_bytes(), original)
        self.assertNotIn(bad, json.dumps(result))

    def test_malformed_stylesheet_href_is_still_missing_resource(self):
        original = b'<html><head><link rel="stylesheet" href="https://[broken/style.css"></head><body>Body</body></html>'
        result = archive_collaboration(Client(html=original), self.note, self.dest)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["malformed_links"], 1)
        self.assertTrue(result["missing_html_resources"])
        self.assertEqual((self.dest / "server-export.html").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
