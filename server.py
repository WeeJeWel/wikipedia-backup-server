"""Download and serve a current, image-free Wikipedia ZIM in one container."""

from __future__ import annotations

import html
import http.client
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo


DATA = Path("/data")
INDEX_URL = "https://download.kiwix.org/zim/wikipedia/"
BACKEND_PORT = 18080
CHUNK = 1024 * 1024
LOG = logging.getLogger("wikipedia-backup-server")
HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "proxy-connection",
}


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.hrefs.extend(value for key, value in attrs if key == "href" and value)


def cron_field(expr: str, minimum: int, maximum: int, names=None) -> set[int]:
    names = names or {}

    def number(value):
        value = value.lower()
        n = names.get(value)
        if n is None:
            if not value.isdecimal():
                raise ValueError(f"invalid cron value: {value}")
            n = int(value)
        if n < minimum or n > maximum:
            raise ValueError(f"cron value out of range: {value}")
        return n

    values = set()
    for item in expr.split(","):
        if not item:
            raise ValueError("empty cron list entry")
        base, sep, step_text = item.partition("/")
        if sep:
            if not step_text.isdecimal() or int(step_text) < 1:
                raise ValueError("cron step must be a positive integer")
            step = int(step_text)
        else:
            step = 1
        if base == "*":
            lo, hi = minimum, maximum
        elif "-" in base:
            left, right = base.split("-", 1)
            lo, hi = number(left), number(right)
            if lo > hi:
                raise ValueError("cron range is reversed")
        else:
            lo = number(base)
            hi = maximum if sep else lo
        values.update(range(lo, hi + 1, step))
    return values


class Schedule:
    """Five-field cron, including Vixie-style day-of-month/day-of-week OR."""

    def __init__(self, expression: str):
        parts = expression.split()
        if len(parts) != 5:
            raise ValueError("SCHEDULE needs five cron fields: minute hour day month weekday")
        months = dict(zip("jan feb mar apr may jun jul aug sep oct nov dec".split(), range(1, 13)))
        days = dict(zip("sun mon tue wed thu fri sat".split(), range(7)))
        self.minute = cron_field(parts[0], 0, 59)
        self.hour = cron_field(parts[1], 0, 23)
        self.dom = cron_field(parts[2], 1, 31)
        self.month = cron_field(parts[3], 1, 12, months)
        self.dow = {day % 7 for day in cron_field(parts[4], 0, 7, days)}
        self.dom_all = parts[2] == "*"
        self.dow_all = parts[4] == "*"

    def matches(self, now: datetime) -> bool:
        dom = now.day in self.dom
        dow = ((now.weekday() + 1) % 7) in self.dow
        if self.dom_all:
            day_matches = dow
        elif self.dow_all:
            day_matches = dom
        else:
            day_matches = dom or dow
        return (now.minute in self.minute and now.hour in self.hour
                and now.month in self.month and day_matches)


def matching_files(data: Path, language: str) -> list[Path]:
    pattern = re.compile(rf"wikipedia_{re.escape(language)}_all_nopic_\d{{4}}-\d{{2}}\.zim")
    return sorted((p for p in data.iterdir() if p.is_file() and pattern.fullmatch(p.name)), reverse=True)


def torrent_info(payload: bytes) -> tuple[str, int]:
    """Read only the identity/size needed to validate a single-file torrent."""
    def item(pos: int, depth: int = 0):
        if depth > 12 or pos >= len(payload):
            raise ValueError("invalid torrent metadata")
        marker = payload[pos:pos + 1]
        if marker == b"i":
            end = payload.index(b"e", pos)
            return int(payload[pos + 1:end]), end + 1
        if marker in (b"l", b"d"):
            values = []
            pos += 1
            while pos < len(payload) and payload[pos:pos + 1] != b"e":
                value, pos = item(pos, depth + 1)
                values.append(value)
            if pos >= len(payload):
                raise ValueError("unterminated torrent metadata")
            if marker == b"l":
                return values, pos + 1
            if len(values) % 2 or any(not isinstance(k, bytes) for k in values[::2]):
                raise ValueError("invalid torrent dictionary")
            return dict(zip(values[::2], values[1::2])), pos + 1
        colon = payload.index(b":", pos)
        size = int(payload[pos:colon])
        end = colon + 1 + size
        if size < 0 or end > len(payload):
            raise ValueError("invalid torrent string")
        return payload[colon + 1:end], end

    metadata, end = item(0)
    if end != len(payload) or not isinstance(metadata, dict):
        raise ValueError("invalid torrent file")
    info = metadata.get(b"info")
    if not isinstance(info, dict) or b"files" in info:
        raise ValueError("expected a single-file torrent")
    name, length = info.get(b"name"), info.get(b"length")
    if not isinstance(name, bytes) or not isinstance(length, int) or length < 1024:
        raise ValueError("torrent has no valid name or length")
    return name.decode("utf-8"), length


def aria2_rpc(port: int, secret: str, method: str, *params):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/jsonrpc",
        data=json.dumps({"jsonrpc": "2.0", "id": "progress", "method": method,
                         "params": ["token:" + secret, *params]}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=2) as response:
        answer = json.load(response)
    if "error" in answer:
        raise RuntimeError(f"aria2 RPC {method}: {answer['error']}")
    return answer["result"]


class App:
    def __init__(self, data: Path, language: str, schedule: str, port: int, tz: str,
                 download_method: str = "torrent"):
        if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z]{2,8})?", language):
            raise ValueError("LANGUAGE must be a Wikipedia language code, such as en or pt")
        if not 1 <= port <= 65535 or port == BACKEND_PORT:
            raise ValueError("PORT must be between 1 and 65535 and differ from the internal port")
        if download_method not in ("torrent", "http"):
            raise ValueError("DOWNLOAD_METHOD must be torrent or http")
        self.data = data
        self.data.mkdir(parents=True, exist_ok=True)
        self.language = language
        self.schedule = Schedule(schedule)
        self.tz = ZoneInfo(tz)
        self.port = port
        self.download_method = download_method
        self.current: Path | None = None
        self.process: subprocess.Popen | None = None
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.stop = threading.Event()
        self.run_check = threading.Event()
        self.status = dict(phase="starting", filename=None, active_filename=None,
                           downloaded=0, total=None, started_at=None, started_bytes=0,
                           download_speed=None, verified_bytes=None, error=None)

    def update_status(self, **changes):
        with self.lock:
            self.status.update(changes)

    def snapshot(self):
        with self.lock:
            result = self.status.copy()
            result["active_filename"] = self.current.name if self.current else None
            result["serving"] = self.process is not None and self.process.poll() is None
        if result["download_speed"] is not None and result["phase"] == "downloading":
            result["bytes_per_second"] = result["download_speed"]
        elif result["started_at"] and result["phase"] == "downloading":
            elapsed = max(time.monotonic() - result["started_at"], 0.001)
            result["bytes_per_second"] = max(0, result["downloaded"] - result["started_bytes"]) / elapsed
        else:
            result["bytes_per_second"] = None
        result.pop("started_at", None)
        result.pop("started_bytes", None)
        result.pop("download_speed", None)
        if result["total"] is not None:
            result["remaining_bytes"] = max(0, result["total"] - result["downloaded"])
        return result

    def update_torrent_progress(self, item):
        total = int(item["totalLength"])
        completed = int(item["completedLength"])
        verified = item.get("verifiedLength")
        phase = ("checking_pieces" if verified is not None else
                 "finishing" if completed >= total else "downloading")
        self.update_status(phase=phase, downloaded=completed, total=total,
                           verified_bytes=int(verified) if verified is not None else None,
                           download_speed=int(item["downloadSpeed"]))

    def latest(self) -> str:
        with urllib.request.urlopen(INDEX_URL, timeout=45) as response:
            page = response.read(2 * 1024 * 1024).decode("utf-8", "replace")
        parser = Links()
        parser.feed(page)
        pat = re.compile(rf"wikipedia_{re.escape(self.language)}_all_nopic_\d{{4}}-\d{{2}}\.zim")
        found = [html.unescape(name) for name in parser.hrefs
                 if pat.fullmatch(html.unescape(name))]
        if not found:
            raise RuntimeError(f"no all_nopic ZIM found for {self.language} at {INDEX_URL}")
        return max(found)

    def verify(self, path: Path):
        self.update_status(phase="verifying", filename=path.name)
        LOG.info("Checking ZIM internal checksum: %s", path.name)
        subprocess.run(["zimcheck", "-C", str(path)], check=True, timeout=7200)

    def start_kiwix(self, path: Path):
        LOG.info("Starting Kiwix for %s", path.name)
        proc = subprocess.Popen([
            "kiwix-serve", f"--address=127.0.0.1", f"--port={BACKEND_PORT}", str(path)
        ])
        for _ in range(300):
            if proc.poll() is not None:
                raise RuntimeError(f"kiwix-serve exited with status {proc.returncode}")
            try:
                conn = http.client.HTTPConnection("127.0.0.1", BACKEND_PORT, timeout=2)
                conn.request("GET", "/")
                response = conn.getresponse()
                response.read()
                conn.close()
                if response.status < 500:
                    return proc
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.2)
        proc.terminate()
        proc.wait(timeout=10)
        raise RuntimeError("kiwix-serve did not become ready")

    def close_kiwix(self):
        with self.lock:
            proc = self.process
            self.process = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def activate(self, path: Path):
        with self.lock:
            old = self.current
        self.close_kiwix()
        try:
            proc = self.start_kiwix(path)
        except Exception:
            if old and old != path and old.exists():
                LOG.exception("Replacement failed; restoring %s", old.name)
                proc = self.start_kiwix(old)
                with self.lock:
                    self.process, self.current = proc, old
            raise
        with self.lock:
            self.process, self.current = proc, path
        self.update_status(phase="ready", filename=path.name, downloaded=0,
                           total=None, started_at=None, download_speed=None, error=None)
        if old and old != path:
            try:
                old.unlink()
                LOG.info("Removed superseded archive %s", old.name)
            except OSError:
                LOG.exception("Could not remove superseded archive %s", old)

    def download(self, name: str) -> Path:
        target = self.data / name
        partial = self.data / (name + ".part")
        marker = self.data / (name + ".part.json")
        url = INDEX_URL + name
        request = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(request, timeout=60) as response:
            expected = int(response.headers["Content-Length"])
            etag = response.headers.get("ETag")
        if expected < 1024:
            raise RuntimeError("archive size is unexpectedly small")

        meta = {"name": name, "expected": expected, "etag": etag}
        existing = partial.stat().st_size if partial.exists() else 0
        try:
            saved_meta = json.loads(marker.read_text())
        except (OSError, ValueError):
            saved_meta = None
        if saved_meta != meta or existing > expected:
            partial.unlink(missing_ok=True)
            existing = 0
        marker.write_text(json.dumps(meta))
        free = shutil.disk_usage(self.data).free
        if free < expected - existing + 1024 ** 3:
            raise RuntimeError("not enough free space for new ZIM plus 1 GiB reserve")

        self.update_status(phase="downloading", filename=name, downloaded=existing,
                           total=expected, started_at=time.monotonic(),
                           started_bytes=existing, download_speed=None, error=None)
        if existing == expected:
            return partial

        headers = {"User-Agent": "wikipedia-backup-server/1.0"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
            if etag:
                headers["If-Range"] = etag
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as response:
            if existing and response.status == 206:
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(f"bytes {existing}-") or not content_range.endswith(f"/{expected}"):
                    raise RuntimeError("server returned a mismatched range; partial file was preserved")
                mode = "ab"
            elif response.status == 200:
                mode = "wb"
                existing = 0
                self.update_status(downloaded=0, started_at=time.monotonic(), started_bytes=0)
            else:
                raise RuntimeError(f"unexpected download response: HTTP {response.status}")
            last_report = 0.0
            with partial.open(mode) as output:
                while not self.stop.is_set():
                    block = response.read(CHUNK)
                    if not block:
                        break
                    output.write(block)
                    existing += len(block)
                    now = time.monotonic()
                    if now - last_report >= 0.5:
                        self.update_status(downloaded=existing)
                        last_report = now
                output.flush()
                os.fsync(output.fileno())
        self.update_status(downloaded=existing)
        if self.stop.is_set():
            raise RuntimeError("download interrupted by shutdown")
        if existing != expected:
            raise RuntimeError(f"incomplete download: {existing} of {expected} bytes")
        return partial

    def download_torrent(self, name: str) -> Path:
        incoming = self.data / ".incoming"
        incoming.mkdir(exist_ok=True)
        torrent = incoming / (name + ".torrent")
        stage = incoming / name
        with urllib.request.urlopen(INDEX_URL + name + ".torrent", timeout=60) as response:
            payload = response.read(4 * 1024 * 1024 + 1)
        if len(payload) > 4 * 1024 * 1024:
            raise RuntimeError("torrent metadata is unexpectedly large")
        torrent_name, expected = torrent_info(payload)
        if torrent_name != name:
            raise RuntimeError("torrent filename does not match the selected ZIM")
        torrent.write_bytes(payload)

        # Reuse the former HTTP downloader's contiguous partial file. aria2
        # checks every torrent piece before accepting any of its bytes.
        old_partial = self.data / (name + ".part")
        if not stage.exists() and old_partial.exists() and old_partial.stat().st_size <= expected:
            old_partial.replace(stage)
            (self.data / (name + ".part.json")).unlink(missing_ok=True)
            LOG.info("Migrated HTTP partial to torrent staging: %s", name)

        allocated = stage.stat().st_blocks * 512 if stage.exists() else 0
        if shutil.disk_usage(self.data).free < expected - allocated + 1024 ** 3:
            raise RuntimeError("not enough free space for new ZIM plus 1 GiB reserve")
        self.update_status(phase="downloading", filename=name, downloaded=0,
                           total=expected, started_at=None, download_speed=0,
                           verified_bytes=None, error=None)

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            rpc_port = sock.getsockname()[1]
        secret = secrets.token_hex(24)
        args = ["aria2c", f"--dir={incoming}", "--check-integrity=true",
                "--continue=true", "--file-allocation=none", "--seed-time=0",
                "--bt-hash-check-seed=false",
                "--bt-stop-timeout=900", "--split=8", "--max-connection-per-server=8",
                "--enable-rpc=true", "--rpc-listen-all=false",
                f"--rpc-listen-port={rpc_port}", f"--rpc-secret={secret}",
                "--console-log-level=warn", "--summary-interval=0", str(torrent)]
        LOG.info("Downloading %s via BitTorrent and its web seed", name)
        proc = subprocess.Popen(args)
        last_progress = time.monotonic()
        last_completed = 0
        confirmed_complete = False
        try:
            while proc.poll() is None and not self.stop.wait(2):
                try:
                    active = aria2_rpc(rpc_port, secret, "aria2.tellActive",
                                       ["totalLength", "completedLength", "downloadSpeed",
                                        "verifiedLength", "seeder"])
                    if active:
                        item = active[0]
                        self.update_torrent_progress(item)
                        completed = int(item["completedLength"])
                        if (completed == expected and item.get("seeder") == "true"
                                and item.get("verifiedLength") is None):
                            confirmed_complete = True
                        if item.get("verifiedLength") is not None:
                            # A full integrity scan may take hours on a Pi.
                            last_progress = time.monotonic()
                        elif completed != last_completed:
                            last_completed = completed
                            last_progress = time.monotonic()
                        elif completed < expected and time.monotonic() - last_progress > 900:
                            raise RuntimeError("torrent made no byte progress for 15 minutes; "
                                               "retrying with the staged file")
                    else:
                        stopped = aria2_rpc(rpc_port, secret, "aria2.tellStopped", 0, 1,
                                            ["status", "errorCode", "errorMessage"])
                        if stopped and stopped[0]["status"] == "complete":
                            confirmed_complete = True
                        elif stopped and stopped[0]["status"] == "error":
                            raise RuntimeError("torrent failed: " +
                                               stopped[0].get("errorMessage", "unknown aria2 error"))
                    if confirmed_complete:
                        LOG.info("Torrent finished; shutting down aria2 to verify the ZIM")
                        aria2_rpc(rpc_port, secret, "aria2.shutdown")
                        break
                except (OSError, ValueError, KeyError):
                    # The local RPC socket might not yet be listening, or the
                    # process may have exited between polling and the request.
                    pass
            if self.stop.is_set() and proc.poll() is None:
                proc.terminate()
            code = proc.wait(timeout=30)
        except Exception:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=30)
            raise
        if self.stop.is_set():
            raise RuntimeError("torrent download interrupted by shutdown")
        if (code != 0 and not confirmed_complete) or not stage.exists() or stage.stat().st_size != expected:
            raise RuntimeError(f"torrent download failed (aria2 exit {code}); staging file retained")
        self.update_status(downloaded=expected, download_speed=0)
        return stage

    def check_once(self):
        with self.operation_lock:
            with self.lock:
                current = self.current
                running = self.process and self.process.poll() is None
            if not running:
                for candidate in matching_files(self.data, self.language):
                    try:
                        self.verify(candidate)
                        self.activate(candidate)
                        break
                    except Exception:
                        LOG.exception("Cannot serve local ZIM %s", candidate.name)
                with self.lock:
                    current = self.current
            self.update_status(phase="checking", error=None)
            name = self.latest()
            if current and name <= current.name:
                self.update_status(phase="ready", filename=current.name)
                return
            target = self.data / name
            if target.exists():
                try:
                    self.verify(target)
                except Exception:
                    invalid = self.data / f"{name}.invalid-{int(time.time())}"
                    target.rename(invalid)
                    LOG.exception("Moved invalid archive to %s", invalid.name)
            if not target.exists():
                partial = (self.download_torrent(name) if self.download_method == "torrent"
                           else self.download(name))
                try:
                    self.verify(partial)
                except Exception:
                    partial.unlink(missing_ok=True)
                    (self.data / ".incoming" / (name + ".aria2")).unlink(missing_ok=True)
                    (self.data / (name + ".part.json")).unlink(missing_ok=True)
                    raise
                os.replace(partial, target)
                (self.data / (name + ".part.json")).unlink(missing_ok=True)
                (self.data / ".incoming" / (name + ".torrent")).unlink(missing_ok=True)
                (self.data / ".incoming" / (name + ".aria2")).unlink(missing_ok=True)
            self.activate(target)

    def worker(self):
        retry_at = None
        while not self.stop.is_set():
            if not self.run_check.wait(1) and (retry_at is None or time.monotonic() < retry_at):
                continue
            self.run_check.clear()
            retry_at = None
            try:
                self.check_once()
            except Exception as exc:
                LOG.exception("Update failed")
                self.update_status(phase="error", error=str(exc))
                with self.lock:
                    has_archive = self.current is not None
                retry_at = time.monotonic() + (3600 if has_archive else 600)

    def scheduler(self):
        previous = None
        while not self.stop.wait(10):
            now = datetime.now(self.tz)
            key = (now.year, now.month, now.day, now.hour, now.minute, now.utcoffset())
            if key != previous and self.schedule.matches(now):
                self.run_check.set()
            previous = key


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Wikipedia backup server</title><style>
body{font:17px/1.5 system-ui,sans-serif;background:#f4f6f8;color:#19242d;margin:0}
main{max-width:660px;margin:10vh auto;padding:2rem;background:white;border-radius:14px;box-shadow:0 8px 30px #15212b15}
h1{font-size:1.7rem;margin:0 0 1rem}p{overflow-wrap:anywhere}
progress{width:100%;height:1.4rem;accent-color:#2563eb}small{color:#56616d}
code{background:#edf2f7;padding:.15rem .3rem;border-radius:4px}
</style></head><body><main><h1>Wikipedia backup server</h1>
<p id="phase">Starting…</p><progress id="bar"></progress>
<p id="detail"></p><p id="error" role="alert"></p>
<small>Updates appear automatically. <a href="/progress">Progress</a> · <a href="/status">JSON status</a></small>
</main><script>
let initiallyWaiting = location.pathname !== '/progress';
function size(n){if(n==null)return 'unknown'; let u=['B','KiB','MiB','GiB','TiB'],i=0;
while(n>=1024&&i<u.length-1){n/=1024;i++}return n.toFixed(i?1:0)+' '+u[i]}
async function refresh(){try{let r=await fetch('/status',{cache:'no-store'}),s=await r.json();
let msg={starting:'Starting',checking:'Checking for an archive',downloading:'Downloading',
checking_pieces:'Checking torrent pieces',finishing:'Finishing torrent',
verifying:'Verifying archive checksum',ready:'Wikipedia is ready',error:'Update needs attention'};
document.getElementById('phase').textContent=(msg[s.phase]||s.phase)+(s.filename?' — '+s.filename:'');
let bar=document.getElementById('bar');bar.removeAttribute('value');
let checking=s.phase==='checking_pieces', downloading=s.phase==='downloading';
let count=checking?s.verified_bytes:s.downloaded;
if((downloading||checking||s.phase==='finishing')&&s.total&&count!=null){bar.max=s.total;bar.value=count}
let pct=s.total&&count!=null?Math.min(count<s.total?99.99:100,100*count/s.total).toFixed(2)+'%':'';
document.getElementById('detail').textContent=downloading?
size(s.downloaded)+' / '+size(s.total)+' ('+pct+') · '+size(s.remaining_bytes)+' remaining'+
(s.bytes_per_second?' · '+size(s.bytes_per_second)+'/s':''):
checking?size(s.verified_bytes)+' / '+size(s.total)+' checked ('+pct+')':
s.phase==='finishing'?size(s.total)+' received; waiting for torrent to finish':
(s.serving?'Currently serving '+s.active_filename:'Waiting for the archive to be ready');
document.getElementById('error').textContent=s.error||'';
if(s.serving&&initiallyWaiting){location.replace('/');return}
}catch(e){document.getElementById('error').textContent='Cannot fetch status: '+e}}
refresh();setInterval(refresh,2000);
</script></body></html>""".encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app

    def respond(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.route()

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def route(self):
        path = self.path.split("?", 1)[0]
        if path == "/status":
            return self.respond(200, json.dumps(self.app.snapshot()).encode(), "application/json")
        if path == "/progress":
            return self.respond(200, PAGE, "text/html; charset=utf-8")
        with self.app.lock:
            ready = self.app.process is not None and self.app.process.poll() is None
        if not ready:
            return self.respond(200, PAGE, "text/html; charset=utf-8")
        self.proxy()

    def proxy(self):
        if not self.path.startswith("/"):
            return self.respond(400, b"Invalid path", "text/plain")
        conn = http.client.HTTPConnection("127.0.0.1", BACKEND_PORT, timeout=120)
        headers_sent = False
        try:
            request_headers = {k: v for k, v in self.headers.items()
                               if k.lower() not in HOP_HEADERS and k.lower() != "content-length"}
            length = int(self.headers.get("Content-Length", "0"))
            if length > 1024 * 1024:
                return self.respond(413, b"Request too large", "text/plain")
            body = self.rfile.read(length) if length else None
            conn.request(self.command, self.path, body=body, headers=request_headers)
            response = conn.getresponse()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            self.close_connection = True
            if self.command != "HEAD":
                while block := response.read(64 * 1024):
                    self.wfile.write(block)
        except (OSError, http.client.HTTPException) as exc:
            LOG.warning("Kiwix proxy error: %s", exc)
            if not headers_sent:
                try:
                    self.respond(503, b"Kiwix is restarting; please retry", "text/plain")
                except OSError:
                    pass
        finally:
            conn.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = App(DATA, os.environ.get("LANGUAGE", "en").lower(),
              os.environ.get("SCHEDULE", "0 3 1 * *"),
              int(os.environ.get("PORT", "8080")),
              os.environ.get("TZ", "Europe/Amsterdam"),
              os.environ.get("DOWNLOAD_METHOD", "torrent").lower())
    server = ThreadingHTTPServer(("0.0.0.0", app.port), Handler)
    server.app = app
    def shutdown(_signum, _frame):
        app.stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    threading.Thread(target=app.worker, daemon=True).start()
    threading.Thread(target=app.scheduler, daemon=True).start()
    app.run_check.set()
    LOG.info("Progress/HTML server listening on port %s; language=%s", app.port, app.language)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        app.stop.set()
        server.server_close()
        app.close_kiwix()


if __name__ == "__main__":
    main()
