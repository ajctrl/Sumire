import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


SCRIPT = Path(__file__).resolve().parents[1] / "resolve-upstream-conflicts.py"
spec = importlib.util.spec_from_file_location("resolver", SCRIPT)
resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resolver)

CONFLICT = "<<<<<<< HEAD\nfork\n||||||| base\nold\n=======\nupstream\n>>>>>>> target\n"


def response(replacements, resolved=True):
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps({
            "resolved": resolved, "replacements": replacements})}]}]}


class ResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Path(self.temp.name) / "budget.json"
        env = patch.dict(os.environ, OPENAI_API_KEY="test-not-a-real-key",
                         OPENAI_BUDGET_FILE=str(self.ledger),
                         OPENAI_DAILY_TOKEN_BUDGET="20000", OPENAI_MODEL="gpt-6.1-sol")
        env.start()
        self.addCleanup(env.stop)
        self.client = resolver.Resolver()

    def test_only_conflict_hunks_change_and_output_is_bounded(self):
        source = "before\n" + CONFLICT + "middle\n" + CONFLICT + "after\n"
        with patch.object(self.client, "request", side_effect=[
                {"input_tokens": 100}, response(["combined", "second\n"])]) as request:
            merged = self.client.resolve("feature.kt", source)
        self.assertEqual(merged, "before\ncombined\nmiddle\nsecond\nafter\n")
        self.assertEqual(request.call_args_list[0].args[0], "responses/input_tokens")
        payload = request.call_args_list[1].args[1]
        self.assertEqual(payload["max_output_tokens"], 8192)
        self.assertNotIn("tools", payload)
        self.assertFalse(payload["store"])
        self.assertEqual(json.loads(self.ledger.read_text())["reserved_tokens"], 8548)

    def test_large_files_send_excerpts_not_the_entire_file(self):
        source = "// unchanged source line\n" * 50000 + CONFLICT + "tail\n"
        with patch.object(self.client, "request", side_effect=[
                {"input_tokens": 200}, response(["merged\n"])]) as request:
            merged = self.client.resolve("IMEService.kt", source)
        self.assertLess(len(request.call_args.args[1]["input"]), 10000)
        self.assertEqual(merged, "// unchanged source line\n" * 50000 + "merged\ntail\n")

    def test_unresolved_bad_count_markers_or_refusal_are_rejected(self):
        for answer in [response([], False), response([]), response(["a", "b"]),
                       response(["<<<<<<< HEAD\n"]), response(["\0"]),
                       {"status": "incomplete"},
                       {"status": "completed", "output": [{"type": "message", "content": [
                           {"type": "refusal", "refusal": "cannot resolve"}]}]}]:
            with self.subTest(answer=answer), patch.object(self.client, "request", side_effect=[
                    {"input_tokens": 1}, answer]):
                self.client.limit = 1_000_000
                with self.assertRaises(RuntimeError):
                    self.client.resolve("feature.kt", CONFLICT)

    def test_budget_blocks_generation_and_survives_new_client(self):
        with patch.object(self.client, "request", side_effect=[
                {"input_tokens": 100}, response(["merged\n"])]):
            self.client.resolve("feature.kt", CONFLICT)
        other = resolver.Resolver()
        other.limit = 16000
        with patch.object(other, "request", return_value={"input_tokens": 100}) as request:
            with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
                other.resolve("feature.kt", CONFLICT)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(json.loads(self.ledger.read_text())["reserved_tokens"], 8548)

    def test_ambiguous_failure_retains_reservation_without_retry(self):
        with patch.object(self.client, "request", side_effect=[
                {"input_tokens": 100}, RuntimeError("connection lost")]) as request:
            with self.assertRaisesRegex(RuntimeError, "connection lost"):
                self.client.resolve("feature.kt", CONFLICT)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(json.loads(self.ledger.read_text())["reserved_tokens"], 8548)

    def test_budget_resets_on_utc_day_change(self):
        self.ledger.write_text(json.dumps({"date": "2000-01-01", "reserved_tokens": 999999}))
        self.client.reserve(20)
        self.assertEqual(json.loads(self.ledger.read_text())["reserved_tokens"], 20)

    def test_midnight_does_not_save_next_days_spending_under_old_cache_key(self):
        with patch.dict(os.environ, OPENAI_BUDGET_DAY="2000-01-01"):
            with self.assertRaisesRegex(RuntimeError, "UTC budget day changed"):
                self.client.reserve(20)
        self.assertFalse(self.ledger.exists())

    def test_unsupported_format_missing_key_and_call_limit_do_not_send_requests(self):
        with patch.object(self.client, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "Unsupported conflict"):
                self.client.resolve("feature.kt", "<<<<<<< HEAD\nincomplete\n")
            self.client.key = ""
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                self.client.resolve("feature.kt", CONFLICT)
            self.client.key = "test"
            self.client.calls = 8
            with self.assertRaisesRegex(RuntimeError, "limits"):
                self.client.resolve("feature.kt", CONFLICT)
        request.assert_not_called()

    def test_http_error_does_not_echo_api_credentials_or_retry(self):
        error = HTTPError("https://api.openai.com/v1/responses", 401,
                          "test-not-a-real-key", {}, None)
        with patch.object(resolver, "urlopen", side_effect=error) as send:
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as caught:
                self.client.request("responses", {})
        send.assert_called_once()
        self.assertNotIn("test-not-a-real-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
