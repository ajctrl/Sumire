from datetime import datetime, timezone
import importlib.util
from io import BytesIO
from pathlib import Path
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError


spec = importlib.util.spec_from_file_location(
    "mirror", Path(__file__).resolve().parents[1] / "gradle-with-maven-mirror.py")
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.path = "/org/robolectric/android-all-instrumented/new-sdk/runtime.jar"
        self.url = mirror.MIRROR + self.path
        self.get = self.enterContext(patch.object(mirror, "urlopen"))
        self.sleep = self.enterContext(patch.object(mirror.time, "sleep"))
        self.enterContext(patch.object(mirror.random, "uniform", return_value=1))

    def error(self, status, headers=None):
        return HTTPError(self.url, status, "fixture", headers or {}, None)

    def test_retries_the_requested_artifact_without_a_version_list(self):
        self.get.side_effect = [self.error(429) for _ in range(4)] + [BytesIO(b"JAR")]
        with mirror.open_artifact(self.path) as response:
            self.assertEqual(response.read(), b"JAR")
        self.assertEqual(self.get.call_args_list, [call(self.url, timeout=90)] * 5)
        self.assertEqual(self.sleep.call_args_list, [call(6), call(11), call(21), call(41)])

    def test_retry_after_and_exhaustion_across_test_classes(self):
        self.get.side_effect = [self.error(429, {"Retry-After": "60"}) for _ in range(5)]
        handler = Mock(path=self.path)
        handler.server.rate_limited_paths = set()
        mirror.MirrorHandler.do_GET(handler)
        mirror.MirrorHandler.do_GET(handler)
        self.assertEqual(handler.send_error.call_args_list, [call(429), call(429)])
        self.assertEqual(self.get.call_count, 5)
        self.assertEqual(self.sleep.call_args_list, [call(60)] * 4)

    def test_retry_after_http_date(self):
        with patch.object(mirror, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 25, tzinfo=timezone.utc)
            self.assertEqual(mirror.retry_delay("Wed, 25 Nov 2026 00:01:30 GMT", 5), 90)
        self.assertEqual(mirror.retry_delay("invalid", 5), 6)

    def test_long_retry_after_returns_429_without_sleeping(self):
        self.get.side_effect = self.error(429, {"Retry-After": "7200"})
        with self.assertRaises(HTTPError) as caught:
            mirror.open_artifact(self.path)
        self.assertEqual(caught.exception.code, 429)
        self.get.assert_called_once()
        self.sleep.assert_not_called()

    def test_retries_share_one_deadline(self):
        clock = self.enterContext(patch.object(mirror.time, "monotonic", return_value=1000))
        def advance_clock(seconds):
            clock.return_value += seconds

        self.sleep.side_effect = advance_clock
        self.get.side_effect = self.error(429, {"Retry-After": "180"})
        with self.assertRaises(HTTPError) as caught:
            mirror.open_artifact(self.path)
        self.assertEqual(caught.exception.code, 429)
        self.assertEqual(self.get.call_count, 2)
        self.sleep.assert_called_once_with(180)

    def test_404_is_not_retried(self):
        self.get.side_effect = self.error(404)
        with self.assertRaises(HTTPError):
            mirror.open_artifact(self.path)
        self.get.assert_called_once()
        self.sleep.assert_not_called()
