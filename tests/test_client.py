"""Network-isolated regression tests for archival integrity and credential scope."""
from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from wizbackup.client import ApiError, AuthError, WizClient, safe_url


AUTH = {
    "token": "test-only-secret-token",
    "kb_guid": "test-kb",
    "kb_server": "https://synthetic-ks.wiz.cn",
}


def response(*, status=200, headers=None, chunks=(), redirect=False):
    result = MagicMock()
    result.status_code = status
    result.headers = headers or {}
    result.is_redirect = redirect
    result.__enter__.return_value = result
    result.__exit__.return_value = False
    result.iter_content.return_value = iter(chunks)
    return result


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.client = WizClient(AUTH, retries=0)
        self.client.session.get = MagicMock()
        self.addCleanup(self.client.session.close)

    def test_urls_require_https_no_userinfo_and_allowed_hostname(self):
        unsafe = [
            "http://synthetic-ks.wiz.cn/object",
            "https://user:password@synthetic-ks.wiz.cn/object",
            "https://wiz.cn.evil.example/object",
            "https://evilwiz.cn/object",
            "https://aliyuncs.com.evil.example/object",
            "file:///tmp/object",
            "https:///missing-host",
        ]
        for url in unsafe:
            with self.subTest(url=url), self.assertRaises(ApiError):
                safe_url(url)
        self.client.session.get.assert_not_called()

    def test_https_wiz_and_oss_urls_are_accepted(self):
        for url in [
            "https://wiz.cn/object",
            "https://synthetic-ks.wiz.cn/object",
            "https://wiz-bucket.oss-cn-beijing.aliyuncs.com/object?signature=example",
        ]:
            self.assertEqual(safe_url(url), url)

    def test_oss_cannot_be_used_as_kb_server(self):
        with self.assertRaises(ApiError):
            WizClient({**AUTH, "kb_server": "https://bucket.oss-cn-beijing.aliyuncs.com"})

    def test_direct_network_ignores_ambient_proxy_configuration(self):
        self.assertFalse(self.client.session.trust_env)
        self.assertEqual(self.client.session.proxies, {})

    def test_us_network_is_local_proxy_only(self):
        client = WizClient(AUTH, network="us")
        self.addCleanup(client.session.close)
        self.assertFalse(client.session.trust_env)
        self.assertEqual(client.session.proxies, {
            "http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897",
        })

    def test_custom_proxy_is_process_local_and_overrides_shortcut(self):
        client = WizClient(AUTH, network="us", proxy="http://127.0.0.1:8123")
        self.addCleanup(client.session.close)
        self.assertFalse(client.session.trust_env)
        self.assertEqual(client.session.proxies, {"http": "http://127.0.0.1:8123", "https": "http://127.0.0.1:8123"})
        direct = WizClient({**AUTH, "proxy": "direct"}, network="us")
        self.addCleanup(direct.session.close)
        self.assertEqual(direct.session.proxies, {})

    def test_invalid_proxy_is_rejected_without_leaking_credentials(self):
        for value in ["", "http://user:synthetic-secret@localhost:8123", "socks5://localhost:8123", "http://localhost:8123/path"]:
            with self.subTest(value=value), self.assertRaises(ApiError) as error:
                WizClient(AUTH, proxy=value)
            self.assertNotIn("synthetic-secret", str(error.exception))

    def test_token_is_sent_only_to_configured_kb_or_account_server(self):
        for host, authorized in [
            ("synthetic-ks.wiz.cn", True),
            ("as.wiz.cn", True),
            ("other.wiz.cn", False),
            ("wiz.cn", False),
            ("bucket.oss-cn-beijing.aliyuncs.com", False),
        ]:
            with self.subTest(host=host):
                self.client.session.get.return_value = response()
                self.client.request(f"https://{host}/object")
                kwargs = self.client.session.get.call_args.kwargs
                self.assertEqual(kwargs["headers"].get("x-wiz-token"),
                                 AUTH["token"] if authorized else None)
                self.assertFalse(kwargs["allow_redirects"])

    def test_redirect_to_oss_drops_token_and_original_query_parameters(self):
        redirect_response = response(status=302, redirect=True, headers={
            "Location": "https://bucket.oss-cn-beijing.aliyuncs.com/blob?signature=example",
        })
        final_response = response()
        self.client.session.get.side_effect = [redirect_response, final_response]
        self.assertIs(self.client.request(AUTH["kb_server"] + "/download",
                                         params={"version": 3}), final_response)
        first, second = self.client.session.get.call_args_list
        self.assertEqual(first.kwargs["headers"]["x-wiz-token"], AUTH["token"])
        self.assertNotIn("x-wiz-token", second.kwargs["headers"])
        self.assertNotIn("Origin", second.kwargs["headers"])
        self.assertIsNone(second.kwargs["params"])
        redirect_response.close.assert_called_once()

    def test_redirect_to_untrusted_host_is_blocked_before_second_request(self):
        initial = response(status=302, redirect=True,
                           headers={"Location": "https://evil.example/collect"})
        self.client.session.get.return_value = initial
        with self.assertRaises(ApiError):
            self.client.request(AUTH["kb_server"] + "/download")
        self.assertEqual(self.client.session.get.call_count, 1)
        initial.close.assert_called_once()

    def test_direct_untrusted_request_never_reaches_transport(self):
        with self.assertRaises(ApiError):
            self.client.request("https://evil.example/collect")
        self.client.session.get.assert_not_called()

    def test_redirect_loop_fails_explicitly(self):
        self.client.session.get.side_effect = [
            response(status=302, redirect=True, headers={"Location": "/again"})
            for _ in range(6)
        ]
        with self.assertRaisesRegex(ApiError, "redirect"):
            self.client.request(AUTH["kb_server"] + "/again")
        self.assertEqual(self.client.session.get.call_count, 6)

    def test_auth_failure_is_not_retried(self):
        self.client.retries = 3
        self.client.session.get.return_value = response(status=401)
        with self.assertRaises(AuthError):
            self.client.request(AUTH["kb_server"] + "/object")
        self.client.session.get.assert_called_once()

    def test_transport_failure_does_not_expose_credentials_or_signed_url(self):
        self.client.session.get.side_effect = requests.ConnectionError(
            "https://server/path?token=" + AUTH["token"])
        with self.assertRaises(ApiError) as caught:
            self.client.request(AUTH["kb_server"] + "/object")
        self.assertNotIn(AUTH["token"], str(caught.exception))
        self.assertNotIn("https://", str(caught.exception))

    def test_version_pagination_continues_past_short_pages_until_empty(self):
        first = [{"version": 2, "docGuid": "one"}]
        second = [{"version": 9, "docGuid": "two"}]
        self.client.api = MagicMock(side_effect=[{"result": first},
                                                {"result": second}, {"result": []}])
        self.assertEqual(list(self.client.version_list("note", page_size=100)),
                         [first, second])
        calls = self.client.api.call_args_list
        self.assertEqual([call.kwargs["version"] for call in calls], [0, 3, 10])
        self.assertTrue(all(call.kwargs["count"] == 100 for call in calls))

    def test_version_pagination_rejects_identical_repeated_page(self):
        page = {"result": [{"version": 1, "docGuid": "one"}]}
        self.client.api = MagicMock(side_effect=[page, page])
        iterator = self.client.version_list("note")
        next(iterator)
        with self.assertRaises(ApiError):
            next(iterator)

    def test_version_pagination_rejects_nonadvancing_changed_page(self):
        self.client.api = MagicMock(side_effect=[
            {"result": [{"version": 10, "docGuid": "one"}]},
            {"result": [{"version": 5, "docGuid": "different"}]},
        ])
        iterator = self.client.version_list("note")
        next(iterator)
        with self.assertRaises(ApiError):
            next(iterator)

    def test_version_pagination_rejects_mixed_regression_even_if_max_advances(self):
        self.client.api = MagicMock(side_effect=[
            {"result": [{"version": 10, "docGuid": "one"}]},
            {"result": [{"version": 8, "docGuid": "stale"},
                        {"version": 20, "docGuid": "new"}]},
        ])
        iterator = self.client.version_list("note")
        next(iterator)
        with self.assertRaises(ApiError):
            next(iterator)

    def test_version_pagination_requires_list_inventory(self):
        self.client.api = MagicMock(return_value={"result": {"version": 1}})
        with self.assertRaises(ApiError):
            list(self.client.version_list("note"))

    def test_download_checks_length_md5_and_installs_private_file(self):
        data = b"archival-object"
        self.client.request = MagicMock(return_value=response(
            headers={"Content-Length": str(len(data))}, chunks=[data[:5], data[5:]]))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "objects" / "note.zip"
            size = self.client.download(AUTH["kb_server"] + "/download", target,
                                        expected_md5=hashlib.md5(data).hexdigest().upper())
            self.assertEqual(size, len(data))
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertFalse(target.with_name("note.zip.part").exists())

    def test_truncated_download_preserves_existing_completed_object(self):
        self.client.request = MagicMock(return_value=response(
            headers={"Content-Length": "100"}, chunks=[b"truncated"]))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.zip"
            target.write_bytes(b"previous-complete-object")
            with self.assertRaisesRegex(ApiError, "size mismatch"):
                self.client.download(AUTH["kb_server"] + "/download", target)
            self.assertEqual(target.read_bytes(), b"previous-complete-object")

    def test_bad_md5_never_installs_object(self):
        self.client.request = MagicMock(return_value=response(chunks=[b"bad-object"]))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.zip"
            with self.assertRaisesRegex(ApiError, "MD5 mismatch"):
                self.client.download(AUTH["kb_server"] + "/download", target,
                                     expected_md5=hashlib.md5(b"correct-object").hexdigest())
            self.assertFalse(target.exists())

    def test_content_encoding_uses_decoded_bytes_for_md5_not_wire_length(self):
        data = b"decoded-object"
        self.client.request = MagicMock(return_value=response(
            headers={"Content-Length": "99", "Content-Encoding": "gzip"}, chunks=[data]))
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.zip"
            self.assertEqual(self.client.download(AUTH["kb_server"] + "/download", target,
                             expected_md5=hashlib.md5(data).hexdigest()), len(data))

    @patch("wizbackup.client.time.sleep")
    def test_download_retries_truncation_and_replaces_part_atomically(self, sleep):
        self.client.retries = 1
        data = b"complete-object"
        self.client.request = MagicMock(side_effect=[
            response(headers={"Content-Length": "99"}, chunks=[b"broken"]),
            response(headers={"Content-Length": str(len(data))}, chunks=[data]),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.zip"
            self.client.download(AUTH["kb_server"] + "/download", target,
                                 expected_md5=hashlib.md5(data).hexdigest())
            self.assertEqual(target.read_bytes(), data)
            self.assertEqual(self.client.request.call_count, 2)
            sleep.assert_called_once()

    def test_interrupted_stream_preserves_previous_object_and_redacts_error(self):
        stream = response()

        def interrupted(_size):
            yield b"partial"
            raise requests.exceptions.ChunkedEncodingError("secret-url?token=" + AUTH["token"])

        stream.iter_content.side_effect = interrupted
        self.client.request = MagicMock(return_value=stream)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "note.zip"
            target.write_bytes(b"previous")
            with self.assertRaisesRegex(ApiError, "Interrupted object download") as caught:
                self.client.download(AUTH["kb_server"] + "/download", target)
            self.assertEqual(target.read_bytes(), b"previous")
            self.assertNotIn(AUTH["token"], str(caught.exception))


if __name__ == "__main__":
    unittest.main()
