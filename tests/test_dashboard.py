"""Dashboard tests open loopback sockets only and use synthetic counters."""
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from wizbackup.dashboard import create_server, status_snapshot


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = create_server(self.root, port=0)
        self.thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, method="GET", headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def save(self, name, data):
        target = self.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data), encoding="utf-8")

    def test_status_is_strictly_sanitized_and_updates_live(self):
        self.save("progress.json", {"phase": "downloading", "total": 100, "completed": 20, "updated": 1000,
                                    "counts": {"exported": 12, "locked": 5, "failed": 3, "secret": "token"},
                                    "title": "PRIVATE-TITLE", "path": "/PRIVATE-PATH", "token": "PRIVATE-TOKEN"})
        self.save("verification.json", {"files_checked": 30, "records": 20, "integrity_ok": False,
                                        "missing_files": ["PRIVATE-NAME"], "notes_without_record": ["PRIVATE-GUID"],
                                        "token": "PRIVATE-TOKEN"})
        code, headers, body = self.request("/status")
        self.assertEqual(200, code)
        self.assertEqual("application/json; charset=utf-8", headers["Content-Type"])
        result = json.loads(body)
        self.assertEqual(20, result["progress"]["completed"])
        self.assertEqual(5, result["progress"]["counts"]["locked"])
        self.assertEqual(1, result["verification"]["missing_files_count"])
        self.assertNotIn(b"PRIVATE", body)
        self.assertNotIn(b"token", body)
        self.save("progress.json", {"phase": "downloaded", "total": 100, "completed": 100})
        self.assertEqual(100, json.loads(self.request("/status")[2])["progress"]["completed"])

    def test_missing_and_transient_partial_files_are_safe(self):
        code, _, body = self.request("/status")
        self.assertEqual(200, code)
        result = json.loads(body)
        self.assertFalse(result["available"])
        self.assertEqual("waiting", result["progress"]["phase"])
        self.assertIsNone(result["verification"])
        (self.root / "progress.json").write_text('{"phase":')
        (self.root / "verification.json").write_text("not-json")
        self.assertFalse(json.loads(self.request("/status")[2])["available"])

    def test_bad_field_types_cannot_reveal_strings_or_nonfinite_json(self):
        self.save("progress.json", {"phase": "PRIVATE-TITLE", "total": "PRIVATE-PATH", "completed": True,
                                    "updated": float("nan"), "counts": {"failed": "PRIVATE-TOKEN", "locked": -4}})
        result = status_snapshot(self.root)
        self.assertEqual("unknown", result["progress"]["phase"])
        self.assertEqual(0, result["progress"]["total"])
        self.assertEqual(0, result["progress"]["completed"])
        self.assertIsNone(result["progress"]["updated"])
        self.assertEqual(0, result["progress"]["counts"]["locked"])
        self.assertNotIn(b"PRIVATE", self.request("/status")[2])

    def test_page_has_progress_refresh_and_no_external_resources(self):
        code, headers, body = self.request("/")
        self.assertEqual(200, code)
        page = body.decode()
        self.assertIn("<progress", page)
        self.assertIn("加密待解", page)
        self.assertIn("setTimeout(refresh, 2000)", page)
        self.assertIn("fetch('/status'", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertNotIn("src=", page)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'sha256-", headers["Content-Security-Policy"])
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_files_directory_listing_and_mutations_are_unavailable(self):
        (self.root / "secret.txt").write_text("PRIVATE")
        for path in ("/secret.txt", "/progress.json", "/verification.json", "/../secret.txt", "/status?extra=1", "/records/"):
            with self.subTest(path=path):
                code, _, body = self.request(path)
                self.assertEqual(404, code)
                self.assertNotIn(b"PRIVATE", body)
        self.assertEqual(405, self.request("/status", method="POST")[0])

    def test_foreign_host_is_rejected_and_external_binding_refused(self):
        self.assertEqual(403, self.request("/status", headers={"Host": "attacker.example"})[0])
        self.assertEqual("127.0.0.1", self.server.server_address[0])
        for host in ("0.0.0.0", "::", "192.168.1.1", "localhost"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    create_server(self.root, host=host, port=0)

    def test_account_totals_remain_unknown_until_every_library_is_scanned(self):
        first = "11111111-1111-1111-1111-111111111111"
        second = "22222222-2222-2222-2222-222222222222"
        self.save("knowledge-bases.json", [
            {"path": "personal", "title": "PRIVATE-TITLE", "token": "PRIVATE-TOKEN"},
            {"path": "teams/" + first, "name": "PRIVATE-TEAM", "kb_guid": first},
            {"path": "teams/" + second, "name": "PRIVATE-TEAM-2", "kb_guid": second},
        ])
        self.save("personal/progress.json", {"phase": "downloaded", "total": 100, "completed": 100,
                                            "counts": {"exported": 95, "locked": 5}, "updated": 10})
        self.save("teams/" + first + "/progress.json", {"phase": "inventory-crosscheck", "discovered": 20, "updated": 20})
        response = self.request("/status")[2]
        result = json.loads(response)
        self.assertEqual("account", result["mode"])
        self.assertEqual(["个人库", "团队库 1", "团队库 2"], [base["label"] for base in result["bases"]])
        self.assertIsNone(result["progress"]["total"])
        self.assertFalse(result["progress"]["total_known"])
        self.assertEqual(100, result["progress"]["known_total"])
        self.assertEqual(100, result["progress"]["completed"])
        self.assertEqual(95, result["progress"]["counts"]["exported"])
        self.assertEqual(2, result["progress"]["unknown_total_bases"])
        self.assertEqual("inventory-crosscheck", result["progress"]["phase"])
        self.assertEqual("not-started", result["bases"][2]["progress"]["phase"])
        self.assertIsNone(result["bases"][2]["progress"]["total"])
        self.assertEqual(20, result["bases"][1]["progress"]["discovered"])
        self.assertNotIn(b"PRIVATE", response)
        self.assertNotIn(first.encode(), response)
        self.assertNotIn(b"teams/", response)
        self.save("teams/" + first + "/progress.json", {"phase": "downloading", "total": 50, "completed": 25,
                                                       "counts": {"exported": 20, "failed": 5}, "updated": 30})
        self.save("teams/" + second + "/progress.json", {"phase": "downloaded", "total": 0, "completed": 0, "updated": 31})
        updated = json.loads(self.request("/status")[2])
        self.assertTrue(updated["progress"]["total_known"])
        self.assertEqual(150, updated["progress"]["total"])
        self.assertEqual(125, updated["progress"]["completed"])
        self.assertEqual(115, updated["progress"]["counts"]["exported"])
        self.assertEqual(5, updated["progress"]["counts"]["failed"])
        self.assertEqual("downloading", updated["progress"]["phase"])

    def test_account_paths_are_whitelisted_and_symlinks_never_read(self):
        guid = "33333333-3333-3333-3333-333333333333"
        paths = ["../outside", "/outside", "personal/subdir", "teams/not-a-guid", "teams/../personal", "teams/" + guid]
        self.save("knowledge-bases.json", [{"path": path} for path in paths])
        with tempfile.TemporaryDirectory() as outside:
            outside = Path(outside)
            (outside / "progress.json").write_text(json.dumps({"phase": "downloading", "total": 424242, "completed": 424242}))
            (self.root / "teams").mkdir()
            (self.root / "teams" / guid).symlink_to(outside, target_is_directory=True)
            result = json.loads(self.request("/status")[2])
            self.assertEqual([], result["bases"])
            self.assertEqual(len(paths), result["invalid_bases"])
            self.assertIsNone(result["progress"]["total"])
            self.assertNotIn("424242", json.dumps(result))
            self.save("knowledge-bases.json", [{"path": "personal"}])
            (self.root / "personal").mkdir()
            (self.root / "personal/progress.json").symlink_to(outside / "progress.json")
            result = status_snapshot(self.root)
            self.assertEqual(1, result["invalid_bases"])
            self.assertEqual([], result["bases"])

    def test_duplicate_account_paths_do_not_double_count(self):
        self.save("knowledge-bases.json", [{"path": "personal"}, {"path": "personal"}])
        self.save("personal/progress.json", {"phase": "downloaded", "total": 10, "completed": 10})
        result = status_snapshot(self.root)
        self.assertEqual(1, len(result["bases"]))
        self.assertEqual(10, result["progress"]["completed"])
        self.assertEqual(1, result["invalid_bases"])
        self.assertFalse(result["progress"]["total_known"])

    def test_account_verification_summary_requires_all_libraries(self):
        guid = "44444444-4444-4444-4444-444444444444"
        self.save("knowledge-bases.json", [{"path": "personal"}, {"path": "teams/" + guid}])
        self.save("personal/verification.json", {"files_checked": 100, "integrity_ok": True, "all_readable": False,
                                                 "missing_files": ["PRIVATE-PATH"], "time": 20})
        partial = status_snapshot(self.root)["verification"]
        self.assertIsNone(partial["integrity_ok"])
        self.assertFalse(partial["all_readable"])
        self.assertEqual(1, partial["verified_bases"])
        self.assertEqual(2, partial["total_bases"])
        self.save("teams/" + guid + "/verification.json", {"files_checked": 50, "integrity_ok": True, "all_readable": True, "time": 30})
        final = status_snapshot(self.root)["verification"]
        self.assertTrue(final["integrity_ok"])
        self.assertEqual(150, final["files_checked"])
        self.assertEqual(30, final["time"])
        self.assertNotIn("PRIVATE", json.dumps(final))

    def test_manifest_transient_errors_and_account_page_stay_safe(self):
        (self.root / "knowledge-bases.json").write_text('[{"path":')
        result = status_snapshot(self.root)
        self.assertEqual("account", result["mode"])
        self.assertIsNone(result["progress"]["total"])
        self.assertEqual(1, result["invalid_bases"])
        page = self.request("/")[2].decode()
        self.assertIn("交叉核对目录", page)
        self.assertIn("已发现，尚未开始", page)
        self.assertIn("总数待确认", page)
        self.assertIn("base-cards", page)

    def test_external_progress_updates_after_note_progress_reaches_100_percent(self):
        self.save("progress.json", {"phase": "mirroring-external", "total": 100, "completed": 100, "updated": 1000})
        self.save("external-resources/progress.json", {"phase": "mirroring", "discovered": 739, "completed": 42,
                                                      "network_bytes": 1048576, "url": "https://PRIVATE-HOST/PRIVATE-PATH",
                                                      "host": "PRIVATE-HOST", "title": "PRIVATE-TITLE", "token": "PRIVATE-TOKEN"})
        path = self.root / "external-resources/progress.json"
        os.utime(path, (2345, 2345))
        response = self.request("/status")[2]
        result = json.loads(response)
        self.assertEqual(result["progress"]["completed"], 100)
        self.assertEqual(result["external_progress"], {"phase": "mirroring", "discovered": 739,
                                                       "completed": 42, "network_bytes": 1048576, "updated": 2345})
        self.assertEqual(result["progress"]["updated"], 2345)
        self.assertNotIn(b"PRIVATE", response)
        self.assertNotIn(b"https://", response)
        self.save("external-resources/progress.json", {"phase": "mirroring", "discovered": 739, "completed": 43})
        os.utime(path, (2346, 2346))
        updated = json.loads(self.request("/status")[2])
        self.assertEqual(updated["external_progress"]["completed"], 43)
        self.assertEqual(updated["progress"]["updated"], 2346)

    def test_external_progress_rejects_private_strings_bad_types_and_nonfinite_numbers(self):
        self.save("external-resources/progress.json", {"phase": "PRIVATE-PHASE", "discovered": "PRIVATE-PATH",
                                                      "completed": True, "network_bytes": float("inf"),
                                                      "updated": "PRIVATE-TIMESTAMP", "manifest": "PRIVATE-PATH"})
        body = self.request("/status")[2]
        external = json.loads(body)["external_progress"]
        self.assertEqual(external["phase"], "unknown")
        self.assertEqual(external["discovered"], 0)
        self.assertEqual(external["completed"], 0)
        self.assertEqual(external["network_bytes"], 0)
        self.assertIsInstance(external["updated"], (float, int))
        self.assertNotIn(b"PRIVATE", body)
        self.assertNotIn(b"Infinity", body)

    def test_final_external_summary_retains_completed_resource_counts(self):
        self.save("external-resources/progress.json", {"phase": "partial", "resources_mirrored": 5,
                                                      "resources_failed": 2, "network_bytes": 4096,
                                                      "manifest": "/PRIVATE-PATH"})
        external = status_snapshot(self.root)["external_progress"]
        self.assertEqual(external["phase"], "partial")
        self.assertEqual(external["discovered"], 7)
        self.assertEqual(external["completed"], 7)
        self.assertEqual(external["network_bytes"], 4096)

    def test_external_directory_and_progress_file_symlinks_are_never_read(self):
        with tempfile.TemporaryDirectory() as outside:
            outside = Path(outside)
            (outside / "progress.json").write_text(json.dumps({"phase": "mirroring", "discovered": 424242,
                                                              "completed": 424242, "host": "PRIVATE-HOST"}))
            for kind in ("directory", "file"):
                with self.subTest(kind=kind):
                    base = self.root / kind
                    base.mkdir()
                    external = base / "external-resources"
                    if kind == "directory":
                        external.symlink_to(outside, target_is_directory=True)
                    else:
                        external.mkdir()
                        (external / "progress.json").symlink_to(outside / "progress.json")
                    result = status_snapshot(base)
                    self.assertIsNone(result["external_progress"])
                    self.assertNotIn("424242", json.dumps(result))
                    self.assertNotIn("PRIVATE", json.dumps(result))

    def test_account_external_progress_sums_safe_counts_and_recent_mtime(self):
        guid = "55555555-5555-5555-5555-555555555555"
        self.save("knowledge-bases.json", [{"path": "personal"}, {"path": "teams/" + guid}])
        self.save("personal/progress.json", {"phase": "mirroring-external", "total": 100, "completed": 100, "updated": 10})
        self.save("teams/" + guid + "/progress.json", {"phase": "downloaded", "total": 20, "completed": 20, "updated": 20})
        self.save("personal/external-resources/progress.json", {"phase": "mirroring", "discovered": 739,
                                                               "completed": 400, "network_bytes": 1024})
        self.save("teams/" + guid + "/external-resources/progress.json", {"phase": "complete", "resources_mirrored": 20,
                                                                         "resources_failed": 0, "network_bytes": 512})
        os.utime(self.root / "personal/external-resources/progress.json", (101, 101))
        os.utime(self.root / ("teams/" + guid + "/external-resources/progress.json"), (100, 100))
        body = self.request("/status")[2]
        result = json.loads(body)
        self.assertEqual(result["external_progress"], {"phase": "mirroring", "discovered": 759,
                                                       "completed": 420, "network_bytes": 1536, "updated": 101})
        self.assertEqual(result["progress"]["updated"], 101)
        self.assertEqual(result["bases"][0]["external_progress"]["completed"], 400)
        self.assertEqual(result["bases"][1]["external_progress"]["completed"], 20)
        self.assertNotIn(guid.encode(), body)
        self.assertNotIn(b"external-resources/", body)

    def test_account_external_completion_waits_for_unstarted_libraries(self):
        guid = "66666666-6666-6666-6666-666666666666"
        self.save("knowledge-bases.json", [{"path": "personal"}, {"path": "teams/" + guid}])
        self.save("personal/progress.json", {"phase": "catalog", "total": 100, "completed": 100})
        self.save("teams/" + guid + "/progress.json", {"phase": "downloaded", "total": 20, "completed": 20})
        self.save("personal/external-resources/progress.json", {"phase": "complete", "resources_mirrored": 10})
        result = status_snapshot(self.root)
        self.assertEqual(result["external_progress"]["phase"], "waiting")
        self.assertEqual(result["external_progress"]["completed"], 10)
        self.assertIsNone(result["bases"][1]["external_progress"])

    def test_external_partial_json_is_safe_and_page_retains_resource_progress(self):
        self.save("external-resources/progress.json", {})
        path = self.root / "external-resources/progress.json"
        path.write_text('{"phase":')
        self.assertIsNone(status_snapshot(self.root)["external_progress"])
        page = self.request("/")[2].decode()
        self.assertIn('id="external-count"', page)
        self.assertIn('id="external-bar"', page)
        self.assertIn("base.external_progress", page)
        self.assertIn("stamp(external.updated)", page)
        self.assertIn("setTimeout(refresh, 2000)", page)


if __name__ == "__main__":
    unittest.main()
