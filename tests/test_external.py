"""External mirroring tests use synthetic notes and mocked public networking."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup

from wizbackup.external import MirrorError, mirror_external


IMAGE = b"\x89PNG\r\n\x1a\nsynthetic-image"
PUBLIC_DNS = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def response(body=IMAGE, content_type="image/png", status=200, location=None, length=None):
    r = MagicMock()
    r.__enter__.return_value = r
    r.__exit__.return_value = False
    r.status_code = status
    r.is_redirect = bool(location)
    r.headers = {"Content-Type": content_type, "Content-Length": str(len(body) if length is None else length)}
    if location:
        r.headers["Location"] = location
    r.iter_content.return_value = iter([body])
    return r


class ExternalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.url = "https://images.example.test/photo.png?source=old"
        self.requests = []
        self.sessions = []
        self.handler = lambda url: response()
        self.dns = patch("wizbackup.external.socket.getaddrinfo", return_value=PUBLIC_DNS).start()
        self.addCleanup(patch.stopall)

        def session():
            s = MagicMock()
            s.headers = {}
            s.cookies = MagicMock()

            def get(url, **kwargs):
                self.requests.append((url, kwargs, s))
                return self.handler(url)

            s.get.side_effect = get
            self.sessions.append(s)
            return s

        patch("wizbackup.external.requests.Session", side_effect=session).start()

    def note(self, *, html=None, markdown=None, external=None, guid="synthetic-guid"):
        dest = self.root / "readable" / guid
        dest.mkdir(parents=True)
        html = html if html is not None else '<html><head></head><body><img src="' + self.url + '"></body></html>'
        (dest / "index.html").write_text(html)
        (dest / "note.md").write_text(markdown if markdown is not None else '![photo](' + self.url + ')\n')
        raw = self.root / "objects" / guid / "1/original.ziw"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"original-note-must-remain-unchanged")
        original = dest / "original/index.html"
        original.parent.mkdir()
        original.write_text(html)
        paths = [dest / "index.html", dest / "note.md", raw, original]
        rec = {"guid": guid, "version": 1, "status": "exported", "warnings": [],
               "readable_path": str(dest.relative_to(self.root)),
               "export": {"status": "exported", "external_resources": external if external is not None else [self.url],
                          "warnings": ["1 external resources were not fetched and are blocked in the offline HTML"]},
               "files": [{"path": str(p.relative_to(self.root)), "size": p.stat().st_size,
                          "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths]}
        records = self.root / "records"
        records.mkdir(exist_ok=True)
        rp = records / (guid + ".json")
        rp.write_text(json.dumps(rec))
        return dest, rp, raw, original

    def assert_record_hashes(self, record_path):
        record = json.loads(record_path.read_text())
        for item in record["files"]:
            p = self.root / item["path"]
            self.assertEqual(item["size"], p.stat().st_size)
            self.assertEqual(item["sha256"], hashlib.sha256(p.read_bytes()).hexdigest())

    def test_mirrors_image_and_rewrites_reading_copies_without_touching_originals(self):
        dest, rp, raw, original = self.note()
        raw_before, html_before = raw.read_bytes(), original.read_bytes()
        result = mirror_external(self.root)
        self.assertEqual(result["resources_mirrored"], 1)
        self.assertEqual(result["resources_failed"], 0)
        src = BeautifulSoup((dest / "index.html").read_text(), "html.parser").img["src"]
        self.assertFalse(src.startswith("https://"))
        self.assertEqual((dest / src).read_bytes(), IMAGE)
        self.assertIn(src, (dest / "note.md").read_text())
        self.assertEqual(raw.read_bytes(), raw_before)
        self.assertEqual(original.read_bytes(), html_before)
        record = json.loads(rp.read_text())
        self.assertEqual(record["export"]["external_resources"], [])
        self.assertEqual(record["export"]["warnings"], [])
        self.assert_record_hashes(rp)

    def test_non_content_links_are_ignored_and_removed_without_network(self):
        urls = ["http://hints.example.test/", "https://feeds.example.test/rss"]
        html = '<head><link rel="dns-prefetch" href="' + urls[0] + '">' \
               '<link rel="preconnect" href="' + urls[0] + '">' \
               '<link rel="alternate" href="' + urls[1] + '">' \
               '<link rel="search" href="' + urls[1] + '"></head><body>Text</body>'
        dest, rp, *_ = self.note(html=html, markdown="Text", external=urls)
        result = mirror_external(self.root)
        self.assertEqual(result["ignored_references"], 4)
        self.assertEqual(self.requests, [])
        self.assertFalse(BeautifulSoup((dest / "index.html").read_text(), "html.parser").find_all("link"))
        self.assertEqual(json.loads(rp.read_text())["export"]["external_resources"], [])

    def test_clean_sessions_use_only_proxy_and_no_credentials_across_redirects(self):
        redirected = "https://cdn.example.test/photo.png"
        self.handler = lambda url: response(status=302, location=redirected) if url == self.url else response()
        self.note()
        mirror_external(self.root, proxy="http://127.0.0.1:7897")
        self.assertEqual([u for u, _, _ in self.requests], [self.url, redirected])
        for url, kwargs, session in self.requests:
            self.assertFalse(session.trust_env)
            self.assertEqual(session.proxies, {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
            self.assertIsNone(session.auth)
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["timeout"], (10, 10))
            for key in ("Cookie", "Referer", "Origin", "Authorization", "x-wiz-token"):
                self.assertNotIn(key, kwargs["headers"])
            self.assertGreaterEqual(session.cookies.clear.call_count, 2)

    def test_redirect_to_private_address_is_blocked_before_private_request(self):
        private = "http://internal.example.test/secret"

        def dns(host, *_args, **_kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))] if host.startswith("internal") else PUBLIC_DNS

        self.dns.side_effect = dns
        self.handler = lambda _url: response(status=302, location=private)
        dest, rp, *_ = self.note()
        result = mirror_external(self.root)
        self.assertEqual(result["resources_failed"], 1)
        self.assertTrue(all(url == self.url for url, _, _ in self.requests))
        self.assertIn(self.url, (dest / "index.html").read_text())
        self.assertTrue(json.loads(rp.read_text())["export"]["warnings"])

    def test_private_link_local_and_nonstandard_ports_never_reach_transport(self):
        for address in ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fe80::1"]:
            with self.subTest(address=address):
                family = socket.AF_INET6 if ":" in address else socket.AF_INET
                self.dns.return_value = [(family, socket.SOCK_STREAM, 6, "", (address, 443))]
                with tempfile.TemporaryDirectory() as tmp:
                    old = self.root
                    self.root = Path(tmp)
                    self.note()
                    mirror_external(self.root)
                    self.root = old
        self.assertEqual(self.requests, [])
        self.dns.return_value = PUBLIC_DNS
        self.url = "https://public.example.test:8080/image.png"
        self.note()
        mirror_external(self.root)
        self.assertEqual(self.requests, [])

    def test_unmapped_wiz_resource_is_reported_without_auth_or_network(self):
        self.url = "https://synthetic-ks.wiz.cn/unmapped-image.png"
        self.note()
        mirror_external(self.root)
        self.assertEqual(self.requests, [])
        manifest = json.loads((self.root / "external-resources/manifest.json").read_text())
        self.assertEqual(manifest["resources"][self.url]["error"], "UnmappedWizResource")

    def test_corrupt_record_is_skipped_instead_of_blessed_with_new_hashes(self):
        dest, rp, *_ = self.note()
        record_before = rp.read_bytes()
        (dest / "note.md").write_text("tampered")
        result = mirror_external(self.root)
        self.assertEqual(result["skipped_invalid_records"], 1)
        self.assertEqual(rp.read_bytes(), record_before)
        self.assertEqual(self.requests, [])

    def test_reading_copy_changed_during_network_is_not_recertified(self):
        dest, rp, *_ = self.note()
        before = rp.read_bytes()

        def handler(_url):
            (dest / "note.md").write_text("modified during download")
            return response()

        self.handler = handler
        result = mirror_external(self.root)
        self.assertEqual(result["skipped_invalid_records"], 1)
        self.assertEqual(rp.read_bytes(), before)
        self.assertEqual((dest / "note.md").read_text(), "modified during download")

    def test_failed_download_keeps_link_and_records_controlled_error(self):
        self.handler = lambda _url: response(status=404)
        dest, rp, *_ = self.note()
        result = mirror_external(self.root)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["resources_failed"], 1)
        self.assertIn(self.url, (dest / "index.html").read_text())
        self.assertIn(self.url, json.loads(rp.read_text())["export"]["external_resources"])
        self.assertNotIn(self.url, json.dumps(result))

    def test_oversized_and_truncated_downloads_are_not_installed(self):
        self.note()
        self.handler = lambda _url: response(length=len(IMAGE) + 10)
        result = mirror_external(self.root)
        self.assertEqual(result["resources_mirrored"], 0)
        manifest = json.loads((self.root / "external-resources/manifest.json").read_text())
        self.assertEqual(manifest["resources"][self.url]["error"], "TruncatedResource")
        self.handler = lambda _url: response(length=100)
        with patch("wizbackup.external.MAX_RESOURCE_BYTES", 10):
            mirror_external(self.root)
        manifest = json.loads((self.root / "external-resources/manifest.json").read_text())
        self.assertEqual(manifest["resources"][self.url]["error"], "ResourceSizeLimit")

    def test_error_page_with_fake_image_mime_is_not_a_successful_image(self):
        self.note()
        self.handler = lambda _url: response(b"not an image", "image/png")
        result = mirror_external(self.root)
        self.assertEqual(result["resources_failed"], 1)
        manifest = json.loads((self.root / "external-resources/manifest.json").read_text())
        self.assertEqual(manifest["resources"][self.url]["error"], "InvalidImageSignature")

    def test_network_exception_details_are_not_exposed_as_error_text(self):
        import requests
        self.note()

        def failure(_url):
            raise requests.ConnectionError("secret signed URL and private query")

        self.handler = failure
        result = mirror_external(self.root)
        manifest = json.loads((self.root / "external-resources/manifest.json").read_text())
        self.assertEqual(manifest["resources"][self.url]["error"], "ConnectionError")
        self.assertNotIn("private query", json.dumps(manifest))

    def test_css_imports_and_assets_are_local_with_original_css_preserved(self):
        css = "https://styles.example.test/main.css"
        child = "https://styles.example.test/nested.css"
        image = "https://styles.example.test/background.png"
        originals = {css: b'@import "nested.css"; body{background:url(background.png)}',
                     child: b"body{color:black}"}

        def handler(url):
            return response(originals[url], "text/css") if url in originals else response()

        self.handler = handler
        dest, rp, *_ = self.note(html='<link rel="stylesheet" href="' + css + '">', markdown="Text", external=[css])
        result = mirror_external(self.root)
        self.assertEqual(result["resources_mirrored"], 3)
        manifest = json.loads((self.root / "external-resources/manifest.json").read_text())
        entry = manifest["resources"][css]
        mirrored = (self.root / entry["path"]).read_text()
        self.assertNotIn("nested.css", mirrored)
        self.assertNotIn("background.png", mirrored)
        self.assertEqual((self.root / entry["source_path"]).read_bytes(), originals[css])
        self.assertEqual(set(entry["dependencies"]), {child, image})
        self.assert_record_hashes(rp)

    def test_css_depth_limit_keeps_unresolved_url_explicit(self):
        css = "https://styles.example.test/0.css"
        self.handler = lambda url: response(('@import "' + str(int(Path(url).stem) + 1) + '.css";').encode(), "text/css")
        _, rp, *_ = self.note(html='<link rel="stylesheet" href="' + css + '">', markdown="Text", external=[css])
        mirror_external(self.root)
        self.assertLessEqual(len(self.requests), 3)
        record = json.loads(rp.read_text())
        self.assertIn("https://styles.example.test/3.css", record["export"]["external_resources"])
        self.assertTrue(record["export"]["warnings"])

    def test_partial_css_retries_dependency_without_redownloading_cached_stylesheet(self):
        css = "https://styles.example.test/main.css"
        dependency = "https://styles.example.test/background.png"
        css_body = b"body{background:url(background.png)}"
        self.handler = lambda url: response(css_body, "text/css") if url == css else response(status=404)
        _, rp, *_ = self.note(html='<link rel="stylesheet" href="' + css + '">', markdown="Text", external=[css])
        mirror_external(self.root)
        self.assertIn(dependency, json.loads(rp.read_text())["export"]["external_resources"])
        self.requests.clear()
        self.handler = lambda _url: response()
        mirror_external(self.root)
        self.assertEqual([url for url, _, _ in self.requests], [dependency])
        self.assertEqual(json.loads(rp.read_text())["export"]["external_resources"], [])
        self.assert_record_hashes(rp)

    def test_native_markdown_images_are_discovered_from_markdown_preview(self):
        dest, rp, *_ = self.note(html="<html><body><pre>Markdown source preview</pre></body></html>")
        result = mirror_external(self.root)
        self.assertEqual(result["resources_mirrored"], 1)
        self.assertNotIn(self.url, (dest / "note.md").read_text())
        self.assertEqual(json.loads(rp.read_text())["export"]["external_resources"], [])

    def test_frontmatter_and_code_examples_are_not_rewritten_or_fetched(self):
        example = "https://examples.example.test/code.png"
        markdown = '---\nsource: "' + self.url + '"\n---\n![photo](' + self.url + ')\n' \
                   '```md\n![example](' + example + ')\n```\n`![example](' + example + ')`\n'
        dest, *_ = self.note(markdown=markdown)
        mirror_external(self.root)
        after = (dest / "note.md").read_text()
        self.assertIn('source: "' + self.url + '"', after)
        self.assertIn('![example](' + example + ')', after)
        self.assertTrue(all(url != example for url, _, _ in self.requests))

    def test_second_run_retains_manifest_and_avoids_network(self):
        self.note()
        mirror_external(self.root)
        self.requests.clear()
        result = mirror_external(self.root)
        self.assertEqual(self.requests, [])
        manifest = json.loads((self.root / "external-resources/manifest.json").read_text())
        self.assertIn(self.url, manifest["resources"])

    def test_verified_url_cache_is_shared_by_another_note_without_network(self):
        self.note()
        mirror_external(self.root)
        self.requests.clear()
        _, rp, *_ = self.note(guid="another-note")
        mirror_external(self.root)
        self.assertEqual(self.requests, [])
        self.assertEqual(json.loads(rp.read_text())["export"]["external_resources"], [])
        self.assert_record_hashes(rp)

    def test_corrupt_url_cache_is_downloaded_again_for_new_note(self):
        self.note()
        mirror_external(self.root)
        manifest = json.loads((self.root / "external-resources/manifest.json").read_text())
        cached = self.root / manifest["resources"][self.url]["path"]
        cached.write_bytes(b"tampered-cache")
        self.requests.clear()
        _, rp, *_ = self.note(guid="another-note")
        mirror_external(self.root)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(cached.read_bytes(), IMAGE)
        self.assert_record_hashes(rp)

    def test_invalid_proxy_cannot_fall_back_to_direct_network(self):
        self.note()
        with self.assertRaises(MirrorError):
            mirror_external(self.root, proxy="")
        self.assertEqual(self.requests, [])

    def test_manifest_cannot_use_original_note_as_css_cache_target(self):
        css = "https://styles.example.test/main.css"
        _, rp, raw, *_ = self.note(html='<link rel="stylesheet" href="' + css + '">', markdown="Text", external=[css])
        before = raw.read_bytes()
        cache = self.root / "external-resources"
        cache.mkdir()
        key = hashlib.sha256(css.encode()).hexdigest()
        source = cache / (key + ".source.css")
        source.write_bytes(b"body{color:red}")
        entry = {"status": "downloaded", "path": str(raw.relative_to(self.root)),
                 "size": raw.stat().st_size, "sha256": hashlib.sha256(before).hexdigest(),
                 "source_path": str(source.relative_to(self.root)), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
        (cache / "manifest.json").write_text(json.dumps({"resources": {css: entry}}))
        self.handler = lambda _url: response(b"body{color:black}", "text/css")
        result = mirror_external(self.root)
        self.assertEqual(result["manifest_schema_errors"], 1)
        self.assertEqual(raw.read_bytes(), before)
        self.assert_record_hashes(rp)

    def test_manifest_cannot_use_original_note_as_css_source(self):
        css = "https://styles.example.test/main.css"
        _, rp, raw, *_ = self.note(html='<link rel="stylesheet" href="' + css + '">', markdown="Text", external=[css])
        before = raw.read_bytes()
        cache = self.root / "external-resources"
        cache.mkdir()
        target = cache / (hashlib.sha256(css.encode()).hexdigest() + ".css")
        target.write_bytes(b"body{color:red}")
        entry = {"status": "downloaded", "path": str(target.relative_to(self.root)),
                 "size": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                 "source_path": str(raw.relative_to(self.root)), "source_sha256": hashlib.sha256(before).hexdigest()}
        (cache / "manifest.json").write_text(json.dumps({"resources": {css: entry}}))
        self.handler = lambda _url: response(b"body{color:black}", "text/css")
        result = mirror_external(self.root)
        self.assertEqual(result["manifest_schema_errors"], 1)
        self.assertEqual(raw.read_bytes(), before)
        self.assert_record_hashes(rp)

    def test_manifest_cache_filename_must_match_exact_url_hash(self):
        self.note()
        cache = self.root / "external-resources"
        cache.mkdir()
        other = cache / ("0" * 64 + ".png")
        other.write_bytes(b"unrelated-file")
        entry = {"status": "downloaded", "path": str(other.relative_to(self.root)),
                 "size": other.stat().st_size, "sha256": hashlib.sha256(other.read_bytes()).hexdigest()}
        (cache / "manifest.json").write_text(json.dumps({"resources": {self.url: entry}}))
        result = mirror_external(self.root)
        self.assertEqual(result["manifest_schema_errors"], 1)
        self.assertEqual(other.read_bytes(), b"unrelated-file")
        self.assertEqual(len(self.requests), 1)

    def test_invalid_manifest_shapes_are_reported_without_crashing(self):
        self.note()
        cache = self.root / "external-resources"
        cache.mkdir()
        invalid = [[], {"resources": []}, {"resources": None}, {"resources": {self.url: []}},
                   {"resources": {self.url: {"status": "downloaded", "size": "wrong-type"}}}]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                (cache / "manifest.json").write_text(json.dumps(value))
                result = mirror_external(self.root)
                self.assertGreaterEqual(result["manifest_schema_errors"], 1)
                manifest = json.loads((cache / "manifest.json").read_text())
                self.assertTrue(manifest["schema_errors"])

    def test_cache_staging_symlink_cannot_overwrite_original_note(self):
        _, _, raw, *_ = self.note()
        before = raw.read_bytes()
        cache = self.root / "external-resources"
        cache.mkdir()
        (cache / (hashlib.sha256(self.url.encode()).hexdigest() + ".png.part")).symlink_to(raw)
        result = mirror_external(self.root)
        self.assertEqual(result["resources_failed"], 1)
        self.assertEqual(raw.read_bytes(), before)

    def test_control_file_staging_symlink_cannot_overwrite_original_note(self):
        _, _, raw, *_ = self.note()
        before = raw.read_bytes()
        cache = self.root / "external-resources"
        cache.mkdir()
        (cache / "progress.json.part").symlink_to(raw)
        with self.assertRaises(MirrorError):
            mirror_external(self.root)
        self.assertEqual(raw.read_bytes(), before)
        self.assertEqual(self.requests, [])

    def test_cache_directory_symlink_is_rejected(self):
        self.note()
        outside = self.root / "objects"
        (self.root / "external-resources").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(MirrorError):
            mirror_external(self.root)
        self.assertFalse((outside / "progress.json").exists())
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
