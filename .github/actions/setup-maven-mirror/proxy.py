"""Serve CI Maven Central downloads, including redirects from the Gradle Plugin Portal."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import random
import shutil
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MIRROR = "https://maven-central.storage-download.googleapis.com/maven2"
PLUGIN_PORTAL = "https://plugins.gradle.org/m2"
BACKOFF = (5, 10, 20, 40)
RETRY_WINDOW_SECONDS = 5 * 60


class MirrorRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.hostname in {"repo.maven.apache.org", "repo1.maven.org"} and target.path.startswith("/maven2/"):
            newurl = MIRROR + target.path.removeprefix("/maven2")
            if target.query:
                newurl += "?" + target.query
        redirect = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirect is not None and req.get_method() == "HEAD":
            redirect.method = "HEAD"
        return redirect


opener = build_opener(MirrorRedirects())


def retry_delay(header, fallback):
    value = (header or "").strip()
    if value.isascii() and value.isdecimal():
        return float(value)
    if value:
        try:
            deadline = parsedate_to_datetime(value)
            return max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return fallback + random.uniform(0, 3)


def open_artifact(url, method):
    deadline = time.monotonic() + RETRY_WINDOW_SECONDS
    for attempt in range(len(BACKOFF) + 1):
        try:
            return opener.open(Request(url, method=method), timeout=90)
        except HTTPError as error:
            error.close()
            if error.code != 429 or attempt == len(BACKOFF):
                raise
            delay = retry_delay(error.headers.get("Retry-After"), BACKOFF[attempt])
            if delay >= deadline - time.monotonic():
                print(f"HTTP 429 for {url}; next retry exceeds the {RETRY_WINDOW_SECONDS}s window", flush=True)
                raise
            print(f"HTTP 429 for {url}; retry {attempt + 1}/{len(BACKOFF)} in {delay:.1f}s", flush=True)
            time.sleep(delay)
            if time.monotonic() >= deadline:
                raise


class MirrorHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.serve_artifact()

    def do_GET(self):
        self.serve_artifact()

    def serve_artifact(self):
        if self.path == "/health":
            self.send_response(204)
            self.end_headers()
            return
        if self.path.startswith("/central/"):
            url = MIRROR + self.path.removeprefix("/central")
        elif self.path.startswith("/plugins/"):
            url = PLUGIN_PORTAL + self.path.removeprefix("/plugins")
        else:
            self.send_error(404)
            return
        # Suppress an immediate new retry loop for each subsequent Robolectric test class.
        if self.server.retry_at.get(url, 0) > time.monotonic():
            self.send_error(429)
            return
        try:
            response = open_artifact(url, self.command)
        except HTTPError as error:
            if error.code == 429:
                cooldown = retry_delay(error.headers.get("Retry-After"), BACKOFF[-1])
                self.server.retry_at[url] = time.monotonic() + max(RETRY_WINDOW_SECONDS, cooldown)
            self.send_error(error.code)
            return
        except OSError as error:
            self.log_error("Mirror request failed: %s", error)
            self.send_error(502)
            return

        with response:
            self.send_response(response.status)
            for header in ("Content-Length", "Content-Type", "Last-Modified", "ETag", "X-Checksum-Sha1"):
                if header in response.headers:
                    self.send_header(header, response.headers[header])
            self.end_headers()
            if self.command == "GET":
                shutil.copyfileobj(response, self.wfile)


if __name__ == "__main__":
    with ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), MirrorHandler) as server:
        server.retry_at = {}
        server.serve_forever()
