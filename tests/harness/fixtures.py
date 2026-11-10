"""Local media fixtures and a range-capable loopback HTTP server (from the original suite).

The server exposes ``/slow/<name>.html`` pages that block until the test releases them, so an
active download can be interrupted at a known moment (graceful stop, unclean crash, slot queue).
"""
from __future__ import annotations

import http.server
import shutil
import subprocess
import threading
from pathlib import Path

INDEX_HTML = ('<html><head><title>Local verification</title></head>'
              '<body><video controls src="/fixture.mp4"></video></body></html>')
WEBM_HTML = ('<html><head><title>Local verification webm</title></head>'
             '<body><video controls src="/fixture.webm"></video></body></html>')


def make_media(fixtures: Path, ffmpeg: str) -> dict:
    """Two-second 320x180 test pattern with a 440 Hz tone: H.264/AAC MP4 and VP9/Opus WebM."""
    fixtures.mkdir(parents=True, exist_ok=True)
    common = ["-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15",
              "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "2"]
    subprocess.run([ffmpeg, *common, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-movflags", "+faststart", str(fixtures / "fixture.mp4")], check=True, timeout=60)
    made = {"mp4": fixtures / "fixture.mp4"}
    webm = subprocess.run([ffmpeg, *common, "-c:v", "libvpx-vp9", "-b:v", "200k", "-deadline", "realtime",
                           "-c:a", "libopus", str(fixtures / "fixture.webm")], timeout=120)
    if webm.returncode == 0:
        made["webm"] = fixtures / "fixture.webm"
    (fixtures / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (fixtures / "webm.html").write_text(WEBM_HTML, encoding="utf-8")
    payload = bytes(range(256)) * 8192
    (fixtures / "fixture.bin").write_bytes(payload)
    made["bin"] = fixtures / "fixture.bin"
    return made


class FixtureServer:
    def __init__(self, fixtures: Path):
        self.fixtures = Path(fixtures)
        self.slow: dict[str, tuple[threading.Event, threading.Event]] = {}
        self.requests: list[str] = []
        owner = self

        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=str(owner.fixtures), **kw)

            def log_message(self, *_):
                pass

            def do_GET(self):
                owner.requests.append(self.path)
                if self.path.startswith("/slow/"):
                    name = self.path[len("/slow/"):].split(".", 1)[0]
                    started, release = owner.slow.setdefault(name, (threading.Event(), threading.Event()))
                    started.set()
                    release.wait(120)
                    self.path = "/index.html"
                try:
                    super().do_GET()
                except ConnectionError:
                    pass  # Expected when an interrupted download's client disappears.

            def send_head(self):
                path = Path(self.translate_path(self.path))
                if not path.is_file():
                    return super().send_head()
                size = path.stat().st_size
                start, end = 0, size - 1
                header = self.headers.get("Range", "")
                if header.startswith("bytes=") and "," not in header:
                    a, b = header[6:].split("-", 1)
                    start, end = (int(a), min(int(b) if b else size - 1, size - 1)) if a else (max(0, size - int(b)), size - 1)
                    if start > end:
                        self.send_error(416)
                        return None
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                else:
                    self.send_response(200)
                self.send_header("Content-Type", self.guess_type(str(path)))
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                self.remaining = end - start + 1
                source = path.open("rb")
                source.seek(start)
                return source

            def copyfile(self, source, output):
                try:
                    if not hasattr(self, "remaining"):
                        return super().copyfile(source, output)
                    while self.remaining > 0:
                        block = source.read(min(65536, self.remaining))
                        if not block:
                            break
                        output.write(block)
                        self.remaining -= len(block)
                except (ConnectionError, OSError):
                    pass  # Probes may close speculative/range connections.

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def slow_url(self, name: str) -> str:
        self.slow.setdefault(name, (threading.Event(), threading.Event()))
        return f"{self.base}/slow/{name}.html"

    def started(self, name: str) -> threading.Event:
        return self.slow.setdefault(name, (threading.Event(), threading.Event()))[0]

    def release(self, name: str | None = None) -> None:
        for key, (_, release) in self.slow.items():
            if name is None or key == name:
                release.set()

    def close(self) -> None:
        self.release()
        self.server.shutdown()
        self.server.server_close()


def which(name: str) -> str | None:
    return shutil.which(name)
