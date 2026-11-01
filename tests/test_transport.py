"""Offline transport/cache boundaries; fake HTTP servers never run commands."""
import contextlib
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest import mock
import urllib.error


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("forge_transport", ROOT / "tools/forge.py")
FORGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FORGE)


@contextlib.contextmanager
def endpoint(redirect=None, as_json=False):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append({"method": self.command, "sid": self.headers.get("mcp-session-id"), "body": body})
            if redirect is not None:
                self.send_response(302)
                self.send_header("Location", redirect)
                self.end_headers()
                return
            value = {"jsonrpc": "2.0", "id": "probe", "result": {"ok": True}}
            encoded = (json.dumps(value) if as_json else "data: " + json.dumps(value) + "\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json" if as_json else "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        do_POST = reply
        do_GET = reply

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port) + "/mcp", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


class EndpointPolicyTests(unittest.TestCase):
    def test_local_endpoints_remain_supported(self):
        for value in [FORGE.DEFAULT_URL, "http://localhost:3141/mcp", "http://[::1]:3141/mcp", "https://127.0.0.1/mcp"]:
            with self.subTest(value=value), mock.patch.object(FORGE, "URL", value):
                self.assertEqual(FORGE._endpoint(), value)

    def test_remote_requires_explicit_https_opt_in(self):
        for value, flag in [("https://example.test/mcp", ""), ("http://example.test/mcp", "1"), ("http://localhost.evil.test/mcp", "1"), ("http://127.1/mcp", "1")]:
            with self.subTest(value=value), mock.patch.object(FORGE, "URL", value), mock.patch.dict(os.environ, {"FORGE_ALLOW_REMOTE": flag}):
                with self.assertRaises(SystemExit):
                    FORGE._endpoint()
        with mock.patch.object(FORGE, "URL", "https://EXAMPLE.test/mcp"), mock.patch.dict(os.environ, {"FORGE_ALLOW_REMOTE": "1"}):
            self.assertEqual(FORGE._endpoint(), "https://example.test/mcp")

    def test_invalid_urls_are_rejected_without_echo_or_request(self):
        values = ["https://user:test-sensitive-marker@example.test/mcp", "https://example.test/mcp?key=test-sensitive-marker", "http://127.0.0.1/mcp#secret", "http://127.0.0.1:bad/mcp", "http://127.0.0.1:0/mcp", "http://127.0.0.1:65536/mcp", "file:///tmp/mcp", "http://127.0.0.1/mcp\nInjected: value", "http://127.0.0.1\\@evil.test/mcp", "http://127.0.0.1/\u0085", "https://example.test/\u2603"]
        for value in values:
            with self.subTest(value=value), mock.patch.object(FORGE, "URL", value), mock.patch.object(FORGE.urllib.request, "build_opener") as opener:
                with self.assertRaises(SystemExit) as caught:
                    FORGE._post({"id": "probe"})
                self.assertNotIn("test-sensitive-marker", str(caught.exception))
                opener.assert_not_called()

    def test_local_post_preserves_protocol_payload(self):
        with endpoint() as (url, seen), mock.patch.object(FORGE, "URL", url):
            reply, _ = FORGE._post({"id": "probe", "method": "fixture"}, "fixture-session")
            self.assertTrue(reply["result"]["ok"])
            self.assertEqual(json.loads(seen[0]["body"]), {"id": "probe", "method": "fixture"})
            self.assertEqual(seen[0]["sid"], "fixture-session")

    def test_json_protocol_response_is_supported(self):
        with endpoint(as_json=True) as (url, seen), mock.patch.object(FORGE, "URL", url):
            reply, _ = FORGE._post({"id": "probe"}, "fixture-session")
            self.assertTrue(reply["result"]["ok"])
            self.assertEqual(len(seen), 1)

    def test_redirect_never_transmits_session_or_payload_to_other_endpoint(self):
        with endpoint() as (other, received), endpoint(other) as (url, initial), mock.patch.object(FORGE, "URL", url):
            with self.assertRaisesRegex(SystemExit, "redirects"):
                FORGE._post({"id": "probe", "params": {"text": "fictional prompt"}}, "fixture-session")
            self.assertEqual(len(initial), 1)
            self.assertEqual(received, [])

    def test_ambient_proxy_cannot_receive_local_daemon_request(self):
        with endpoint() as (proxy, received), endpoint() as (url, direct), mock.patch.object(FORGE, "URL", url), mock.patch.dict(os.environ, {"http_proxy": proxy, "HTTP_PROXY": proxy, "no_proxy": "", "NO_PROXY": ""}):
            FORGE._post({"id": "probe"}, "fixture-session")
            self.assertEqual(len(direct), 1)
            self.assertEqual(received, [])

    def test_network_errors_do_not_echo_endpoint_or_reason(self):
        with mock.patch.object(FORGE, "_sid", return_value="fixture-session"), mock.patch.object(FORGE, "_post", side_effect=urllib.error.URLError("test-sensitive-marker")):
            with self.assertRaises(SystemExit) as caught:
                FORGE.call("fixture", {})
            self.assertNotIn("test-sensitive-marker", str(caught.exception))

    def test_protocol_and_read_errors_are_redacted_and_not_retried(self):
        for error in [http.client.BadStatusLine("test-sensitive-marker"), ConnectionResetError("test-sensitive-marker"), json.JSONDecodeError("test-sensitive-marker", "fixture", 0)]:
            with self.subTest(kind=type(error).__name__), mock.patch.object(FORGE, "_sid", return_value="fixture"), mock.patch.object(FORGE, "_post", side_effect=error) as post, mock.patch.object(FORGE, "_handshake") as handshake:
                with self.assertRaises(SystemExit) as caught:
                    FORGE.call("fixture", {})
                self.assertNotIn("test-sensitive-marker", str(caught.exception))
                self.assertIn("not retried", str(caught.exception))
                post.assert_called_once()
                handshake.assert_not_called()

    def test_rpc_error_diagnostics_do_not_echo_unrequested_server_data(self):
        messages = [{"error": {"code": -32603, "message": "test-sensitive-marker"}}, {"result": {"isError": True, "content": [{"type": "text", "text": "test-sensitive-marker"}]}}, {"result": {"content": [{"type": "text", "text": {"private": "test-sensitive-marker"}}]}}]
        for reply in messages:
            with self.subTest(shape=list(reply)), mock.patch.object(FORGE, "_sid", return_value="fixture"), mock.patch.object(FORGE, "_post", return_value=(reply, None)):
                with self.assertRaises(SystemExit) as caught:
                    FORGE.call("fixture", {})
                self.assertNotIn("test-sensitive-marker", str(caught.exception))

    def test_uncertain_http_errors_never_repeat_a_command(self):
        for code in [400, 401, 403, 429, 500, 503]:
            error = urllib.error.HTTPError("https://example.test/private", code, "test-sensitive-marker", {}, None)
            with self.subTest(code=code), mock.patch.object(FORGE, "_sid", return_value="fixture-session"), mock.patch.object(FORGE, "_post", side_effect=error) as post, mock.patch.object(FORGE, "_handshake") as handshake:
                with self.assertRaises(SystemExit) as caught:
                    FORGE.call("fixture", {})
                self.assertIn("not retried", str(caught.exception))
                self.assertNotIn("test-sensitive-marker", str(caught.exception))
                post.assert_called_once()
                handshake.assert_not_called()

    def test_expired_session_has_one_bounded_protocol_retry(self):
        expired = urllib.error.HTTPError(FORGE.DEFAULT_URL, 404, "expired", {}, None)
        good = {"result": {"content": [{"type": "text", "text": "{}"}]}}
        with mock.patch.object(FORGE, "_sid", return_value="old"), mock.patch.object(FORGE, "_handshake", return_value="fresh") as handshake, mock.patch.object(FORGE, "_post", side_effect=[expired, (good, None)]) as post:
            self.assertEqual(FORGE.call("fixture", {}), {})
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args.args[1], "fresh")
            handshake.assert_called_once_with()


class SessionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "mcp-session"
        patch = mock.patch.object(FORGE, "SESSION_FILE", str(self.path))
        patch.start()
        self.addCleanup(patch.stop)

    def test_cache_is_private_and_bound_to_exact_endpoint(self):
        FORGE._save_session(FORGE.DEFAULT_URL, "fixture-session")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(FORGE._cached_session(FORGE.DEFAULT_URL), "fixture-session")
        self.assertIsNone(FORGE._cached_session("http://127.0.0.1:3142/mcp"))
        self.assertEqual(set(json.loads(self.path.read_text())), {"version", "endpoint_sha256", "session_id"})
        self.assertNotIn(FORGE.DEFAULT_URL, self.path.read_text())

    def test_endpoint_switch_handshakes_without_reusing_other_session(self):
        FORGE._save_session(FORGE.DEFAULT_URL, "old-endpoint-session")
        with mock.patch.object(FORGE, "URL", "http://127.0.0.1:3142/mcp"), mock.patch.object(FORGE, "_handshake", return_value="new-session") as handshake:
            self.assertEqual(FORGE._sid(), "new-session")
            handshake.assert_called_once_with()

    def test_legacy_unscoped_and_insecure_cache_are_not_reused(self):
        for content, mode in [("legacy-session", 0o600), (json.dumps({"version": 1, "endpoint_sha256": hashlib.sha256(FORGE.DEFAULT_URL.encode()).hexdigest(), "session_id": "old"}), 0o644), ("{malformed", 0o600)]:
            self.path.write_text(content)
            self.path.chmod(mode)
            with self.subTest(mode=mode), mock.patch.object(FORGE, "_handshake", return_value="fresh"):
                self.assertEqual(FORGE._sid(), "fresh")

    def test_symlink_cache_is_refused_without_touching_target(self):
        target = Path(self.temp.name) / "unrelated"
        target.write_text("preserve")
        self.path.symlink_to(target)
        for operation in [lambda: FORGE._cached_session(FORGE.DEFAULT_URL), lambda: FORGE._save_session(FORGE.DEFAULT_URL, "fixture")]:
            with self.assertRaises(SystemExit):
                operation()
        self.assertEqual(target.read_text(), "preserve")

    def test_hardlink_and_fifo_cache_are_refused(self):
        target = Path(self.temp.name) / "unrelated"
        target.write_text("preserve")
        os.link(target, self.path)
        with self.assertRaises(SystemExit):
            FORGE._cached_session(FORGE.DEFAULT_URL)
        self.path.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(SystemExit):
            FORGE._cached_session(FORGE.DEFAULT_URL)

    def test_untrusted_cache_directory_is_refused(self):
        os.chmod(self.temp.name, 0o777)
        try:
            with self.assertRaises(SystemExit):
                FORGE._save_session(FORGE.DEFAULT_URL, "fixture")
        finally:
            os.chmod(self.temp.name, 0o700)

    def test_malformed_session_header_is_refused(self):
        with self.assertRaises(SystemExit):
            FORGE._save_session(FORGE.DEFAULT_URL, "fixture\ninjected")
        self.assertFalse(self.path.exists())

    def test_atomic_refresh_does_not_leave_extra_cache_files(self):
        FORGE._save_session(FORGE.DEFAULT_URL, "old")
        FORGE._save_session(FORGE.DEFAULT_URL, "new")
        self.assertEqual(FORGE._cached_session(FORGE.DEFAULT_URL), "new")
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_reader_survives_concurrent_atomic_cache_refresh(self):
        FORGE._save_session(FORGE.DEFAULT_URL, "old")
        original_fstat = os.fstat
        swapped = False

        def refresh_after_open(fd):
            nonlocal swapped
            info = original_fstat(fd)
            if stat.S_ISREG(info.st_mode) and not swapped:
                swapped = True
                replacement = self.path.with_name("replacement")
                replacement.write_text(json.dumps({"version": 1, "endpoint_sha256": hashlib.sha256(FORGE.DEFAULT_URL.encode()).hexdigest(), "session_id": "new"}))
                replacement.chmod(0o600)
                os.replace(replacement, self.path)
            return original_fstat(fd)

        with mock.patch.object(FORGE.os, "fstat", side_effect=refresh_after_open):
            self.assertEqual(FORGE._cached_session(FORGE.DEFAULT_URL), "old")
        self.assertTrue(swapped)
        self.assertEqual(FORGE._cached_session(FORGE.DEFAULT_URL), "new")


if __name__ == "__main__":
    unittest.main()
