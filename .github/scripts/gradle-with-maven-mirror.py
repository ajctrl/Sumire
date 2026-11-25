"""Run CI tests with per-download retries for Robolectric mirror rate limits."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import random
import shutil
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import urlopen


MIRROR = "https://maven-central.storage-download.googleapis.com/maven2"
# Gradle fingerprints the URL passed to the test JVM; each CI job has its own runner.
MIRROR_PORT = 18765
BACKOFF = (5, 10, 20, 40)
RETRY_WINDOW_SECONDS = 5 * 60


def retry_delay(header, fallback):
    value = (header or "").strip()
    if value.isascii() and value.isdecimal():
        return int(value)
    if value:
        try:
            deadline = parsedate_to_datetime(value)
            return max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return fallback + random.uniform(0, 3)


def open_artifact(path):
    deadline = time.monotonic() + RETRY_WINDOW_SECONDS
    for attempt in range(len(BACKOFF) + 1):
        try:
            return urlopen(MIRROR + path, timeout=90)
        except HTTPError as error:
            error.close()
            if error.code != 429 or attempt == len(BACKOFF):
                raise
            delay = retry_delay(error.headers.get("Retry-After"), BACKOFF[attempt])
            if delay >= deadline - time.monotonic():
                print(f"HTTP 429 for {path}; next retry exceeds the {RETRY_WINDOW_SECONDS}s window", flush=True)
                raise
            print(f"HTTP 429 for {path}; retry {attempt + 1}/{len(BACKOFF)} in {delay:.1f}s", flush=True)
            time.sleep(delay)
            if time.monotonic() >= deadline:
                raise


class MirrorHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/"):
            self.send_error(400)
            return
        # Later test classes can ask for the same failed dependency again.
        if self.path in self.server.rate_limited_paths:
            self.send_error(429)
            return
        try:
            response = open_artifact(self.path)
        except HTTPError as error:
            if error.code == 429:
                self.server.rate_limited_paths.add(self.path)
            self.send_error(error.code)
            return
        except OSError as error:
            self.log_error("Mirror request failed: %s", error)
            self.send_error(502)
            return

        with response:
            self.send_response(response.status)
            if response.headers.get("Content-Length") is not None:
                self.send_header("Content-Length", response.headers["Content-Length"])
            self.end_headers()
            shutil.copyfileobj(response, self.wfile)


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", MIRROR_PORT), MirrorHandler) as server:
        server.rate_limited_paths = set()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            init_script = Path(__file__).resolve().with_name("robolectric-mirror.init.gradle")
            sys.exit(subprocess.run([
                "./gradlew", "--init-script", str(init_script),
                f"-ProbolectricMirrorUrl=http://127.0.0.1:{server.server_port}", *sys.argv[1:],
            ]).returncode)
        finally:
            server.shutdown()
            thread.join()
