import http.client
import io
import json
import tempfile
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import server


NAME = "wikipedia_en_all_nopic_2026-06.zim"
CONTENT = b"test-zim-content" * 256


def bencode(value):
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in sorted(value.items())) + b"e"
    raise TypeError(value)


def fake_torrent(name=NAME, length=len(CONTENT)):
    return bencode({b"info": {b"name": name.encode(), b"length": length,
                               b"piece length": 4096, b"pieces": b"0" * 20}})


class Catalog(BaseHTTPRequestHandler):
    transferred = []

    def handle_request(self):
        if self.path == "/":
            body = (f'<a href="wikipedia_en_all_mini_2026-09.zim">mini</a>'
                    f'<a href="{NAME}">nopic</a>').encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command == "GET":
                self.wfile.write(body)
            return
        if self.path != "/" + NAME:
            self.send_error(404)
            return
        start = 0
        if self.headers.get("Range"):
            start = int(self.headers["Range"].split("=")[1].split("-")[0])
            self.transferred.append(start)
        self.send_response(206 if start else 200)
        self.send_header("Content-Length", str(len(CONTENT) - start))
        self.send_header("ETag", '"test-file"')
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(CONTENT)-1}/{len(CONTENT)}")
        self.end_headers()
        if self.command == "GET":
            self.wfile.write(CONTENT[start:])

    do_GET = handle_request
    do_HEAD = handle_request

    def log_message(self, *_):
        pass


class FakeProcess:
    def poll(self):
        return None

    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0


class Backend(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ("Kiwix article: " + self.path).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.catalog = ThreadingHTTPServer(("127.0.0.1", 0), Catalog)
        self.addCleanup(self.catalog.server_close)
        self.addCleanup(self.catalog.shutdown)
        threading.Thread(target=self.catalog.serve_forever, daemon=True).start()
        self.index = f"http://127.0.0.1:{self.catalog.server_port}/"
        patch = mock.patch.object(server, "INDEX_URL", self.index)
        patch.start()
        self.addCleanup(patch.stop)
        self.app = server.App(Path(self.temp.name), "en", "0 3 1 * *", 8080,
                              "Europe/Amsterdam")

    def test_cron_first_of_month_local_time(self):
        zone = ZoneInfo("Europe/Amsterdam")
        self.assertTrue(self.app.schedule.matches(datetime(2026, 10, 1, 3, 0, tzinfo=zone)))
        self.assertFalse(self.app.schedule.matches(datetime(2026, 10, 1, 2, 0, tzinfo=zone)))
        self.assertFalse(self.app.schedule.matches(datetime(2026, 10, 2, 3, 0, tzinfo=zone)))
        both = server.Schedule("*/15 1-5 1 * sun")
        self.assertTrue(both.matches(datetime(2026, 11, 8, 3, 15, tzinfo=zone)))
        with self.assertRaises(ValueError):
            server.Schedule("0 25 1 * *")

    def test_discovery_and_resume(self):
        self.assertEqual(self.app.latest(), NAME)
        part = self.app.data / (NAME + ".part")
        part.write_bytes(CONTENT[:100])
        (self.app.data / (NAME + ".part.json")).write_text(json.dumps({
            "name": NAME, "expected": len(CONTENT), "etag": '"test-file"'}))
        Catalog.transferred.clear()
        self.assertEqual(self.app.download(NAME).read_bytes(), CONTENT)
        self.assertEqual(Catalog.transferred, [100])

    def test_torrent_metadata_and_http_partial_migration(self):
        self.assertEqual(server.torrent_info(fake_torrent()), (NAME, len(CONTENT)))
        with self.assertRaisesRegex(ValueError, "single-file"):
            server.torrent_info(bencode({b"info": {b"name": b"bad", b"files": []}}))
        partial = self.app.data / (NAME + ".part")
        partial.write_bytes(CONTENT[:100])

        class FinishedProcess:
            def __init__(self, args):
                self.args = args
                stage = self_outer.app.data / ".incoming" / NAME
                self_outer.assertEqual(stage.read_bytes(), CONTENT[:100])
                stage.write_bytes(CONTENT)

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        self_outer = self
        with mock.patch.object(server.urllib.request, "urlopen", return_value=io.BytesIO(fake_torrent())), \
             mock.patch.object(server.subprocess, "Popen", side_effect=FinishedProcess) as launched:
            stage = self.app.download_torrent(NAME)
        self.assertFalse(partial.exists())
        self.assertEqual(stage.read_bytes(), CONTENT)
        args = launched.call_args.args[0]
        self.assertIn("--check-integrity=true", args)
        self.assertIn("--seed-time=0", args)
        self.assertIn("--rpc-listen-all=false", args)

    def test_first_visit_shows_progress_and_status(self):
        ui = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        ui.app = self.app
        self.addCleanup(ui.server_close)
        self.addCleanup(ui.shutdown)
        threading.Thread(target=ui.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", ui.server_port)
        conn.request("GET", "/wiki/Raspberry_Pi")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b"Wikipedia backup server", response.read())
        conn.request("GET", "/status")
        status = json.loads(conn.getresponse().read())
        self.assertFalse(status["serving"])
        conn.close()

    def test_failed_replacement_keeps_existing_server(self):
        old = self.app.data / "wikipedia_en_all_nopic_2026-03.zim"
        old.write_bytes(b"old")
        self.app.current = old
        self.app.process = FakeProcess()
        with mock.patch.object(self.app, "download_torrent", side_effect=RuntimeError("no space")):
            with self.assertRaisesRegex(RuntimeError, "no space"):
                self.app.check_once()
        self.assertIs(self.app.current, old)
        self.assertIsNotNone(self.app.process)
        self.assertTrue(old.exists())

    def test_proxy_routes_to_running_kiwix_and_progress_stays_available(self):
        backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        self.addCleanup(backend.server_close)
        self.addCleanup(backend.shutdown)
        threading.Thread(target=backend.serve_forever, daemon=True).start()
        patch = mock.patch.object(server, "BACKEND_PORT", backend.server_port)
        patch.start()
        self.addCleanup(patch.stop)
        self.app.current = self.app.data / NAME
        self.app.process = FakeProcess()
        ui = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        ui.app = self.app
        self.addCleanup(ui.server_close)
        self.addCleanup(ui.shutdown)
        threading.Thread(target=ui.serve_forever, daemon=True).start()
        conn = http.client.HTTPConnection("127.0.0.1", ui.server_port)
        conn.request("GET", "/wiki/Raspberry_Pi?test=1")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"Kiwix article: /wiki/Raspberry_Pi?test=1")
        conn.close()
        conn = http.client.HTTPConnection("127.0.0.1", ui.server_port)
        conn.request("GET", "/progress")
        self.assertIn(b"Wikipedia backup server", conn.getresponse().read())
        conn.close()

    def test_old_archive_removed_only_after_new_server_ready(self):
        old = self.app.data / "wikipedia_en_all_nopic_2026-03.zim"
        old.write_bytes(b"old")
        new = self.app.data / NAME
        new.write_bytes(CONTENT)
        self.app.current = old
        self.app.process = FakeProcess()
        with mock.patch.object(self.app, "start_kiwix", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                self.app.activate(new)
        self.assertTrue(old.exists())
        with mock.patch.object(self.app, "start_kiwix", return_value=FakeProcess()):
            self.app.activate(new)
        self.assertFalse(old.exists())
        self.assertEqual(self.app.current, new)


if __name__ == "__main__":
    unittest.main()
