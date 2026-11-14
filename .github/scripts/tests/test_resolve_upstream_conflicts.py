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
    return {"status": "completed", "usage": {"total_tokens": 300}, "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps({
            "resolved": resolved, "requires_review": False, "reason": "Routine compatible change", "replacements": replacements})}]}]}


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
            merged = self.client.resolve("docs/guide.md", source)
        self.assertEqual(merged, "before\ncombined\nmiddle\nsecond\nafter\n")
        self.assertEqual(request.call_args_list[0].args[0], "responses/input_tokens")
        payload = request.call_args_list[1].args[1]
        self.assertEqual(payload["max_output_tokens"], 19644)
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertNotIn("tools", payload)
        self.assertFalse(payload["store"])
        self.assertEqual(json.loads(self.ledger.read_text())["used_tokens"], 300)

    def test_large_files_send_excerpts_not_the_entire_file(self):
        source = "// unchanged source line\n" * 50000 + CONFLICT + "tail\n"
        with patch.object(self.client, "request", side_effect=[
                {"input_tokens": 200}, response(["merged\n"])]) as request:
            merged = self.client.resolve("docs/large-guide.md", source)
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
                    self.client.resolve("docs/guide.md", CONFLICT)

    def test_budget_blocks_generation_and_survives_new_client(self):
        with patch.object(self.client, "request", side_effect=[
                {"input_tokens": 100}, response(["merged\n"])]):
            self.client.resolve("docs/guide.md", CONFLICT)
        other = resolver.Resolver()
        other.limit = 400
        with patch.object(other, "request", return_value={"input_tokens": 100}) as request:
            with self.assertRaisesRegex(resolver.BudgetPaused, "Daily allowance reached"):
                other.resolve("docs/other.md", CONFLICT)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(json.loads(self.ledger.read_text())["used_tokens"], 300)

    def test_ambiguous_failure_retains_reservation_without_retry(self):
        with patch.object(self.client, "request", side_effect=[
                {"input_tokens": 100}, RuntimeError("connection lost")]) as request:
            with self.assertRaisesRegex(RuntimeError, "connection lost"):
                self.client.resolve("docs/guide.md", CONFLICT)
        self.assertEqual(request.call_count, 2)
        state = json.loads(self.ledger.read_text())
        self.assertEqual(state["used_tokens"], 0)
        self.assertEqual(sum(state["pending"].values()), 20000)

    def test_budget_resets_on_utc_day_change(self):
        self.client.state.data.update(date="2000-01-01", used_tokens=999999)
        self.client.reserve(20)
        saved = json.loads(self.ledger.read_text())
        self.assertEqual(saved["used_tokens"], 0)
        self.assertEqual(sum(saved["pending"].values()), 20)

    def test_day_change_during_checkpoint_upload_does_not_start_generation(self):
        day = self.client.state.data["date"]
        with patch.object(resolver.repair_state, "today", side_effect=[day, "2000-01-01"]):
            with self.assertRaisesRegex(resolver.BudgetPaused, "UTC day changed"):
                self.client.reserve(20)
        self.assertEqual(sum(json.loads(self.ledger.read_text())["pending"].values()), 20)

    def test_unsupported_format_and_missing_key_do_not_send_requests(self):
        with patch.object(self.client, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "ordinary diff3 conflict"):
                self.client.resolve("docs/guide.md", "<<<<<<< HEAD\nincomplete\n")
            self.client.key = ""
            with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
                self.client.resolve("docs/guide.md", CONFLICT)
        request.assert_not_called()

    def test_actual_usage_including_reasoning_replaces_the_reservation(self):
        answer = response(["merged\n"])
        answer["usage"] = {"total_tokens": 300}
        with patch.object(self.client, "request", side_effect=[{"input_tokens": 100}, answer]):
            self.client.resolve("docs/guide.md", CONFLICT)
        saved = json.loads(self.ledger.read_text())
        self.assertEqual(saved["used_tokens"], 300)
        self.assertEqual(saved["pending"], {})

    def test_saved_results_are_revalidated_and_reused_without_api_calls(self):
        with patch.object(self.client, "request", side_effect=[{"input_tokens": 100}, response(["merged\n"])]):
            self.client.resolve("docs/guide.md", CONFLICT)
        other = resolver.Resolver()
        with patch.object(other, "request") as request:
            self.assertEqual(other.resolve("docs/guide.md", CONFLICT), "merged\n")
        request.assert_not_called()
        # A different target revision must never inherit this result.
        another = resolver.Resolver(task="different-revisions")
        with patch.object(another, "request", side_effect=[{"input_tokens": 100}, response(["new\n"])]) as request:
            self.assertEqual(another.resolve("docs/guide.md", CONFLICT), "new\n")
        self.assertEqual(request.call_count, 2)

    def test_fresh_runner_restores_repairs_and_same_day_usage_from_artifact(self):
        answer = response(["merged\n"])
        answer["usage"] = {"total_tokens": 9700}
        with patch.object(self.client, "request", side_effect=[{"input_tokens": 100}, answer]):
            self.client.resolve("docs/guide.md", CONFLICT)
        saved = json.loads(self.ledger.read_text())
        fresh_file = Path(self.temp.name) / "fresh-runner" / "repair-state.json"
        with patch.dict(os.environ, OPENAI_BUDGET_FILE=str(fresh_file),
                        OPENAI_DAILY_TOKEN_BUDGET="10000", SUMIRE_ARTIFACT_CLIENT="mock-client"), \
                patch.object(resolver.repair_state, "restore_remote", return_value=saved) as restore:
            fresh = resolver.Resolver()
            with patch.object(fresh, "request", return_value={"input_tokens": 100}) as request:
                self.assertEqual(fresh.resolve("docs/guide.md", CONFLICT), "merged\n")
                request.assert_not_called()
                with self.assertRaises(resolver.BudgetPaused):
                    fresh.resolve("docs/next.md", CONFLICT)
                self.assertEqual(request.call_count, 1)  # Counting only; no generation.
        restore.assert_called_once()
        self.assertEqual(fresh.state.data["used_tokens"], 9700)

    def test_full_model_output_limit_is_available_on_the_first_request(self):
        self.client.limit = 200000
        with patch.object(self.client, "request", side_effect=[{"input_tokens": 100}, response(["merged\n"])]) as request:
            self.client.resolve("docs/guide.md", CONFLICT)
        self.assertEqual(request.call_args.args[1]["max_output_tokens"], 128000)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.client.state.data["used_tokens"], 300)

    def test_output_allowance_accounts_for_spent_and_pending_tokens(self):
        self.client.state.data.update(used_tokens=1000, pending={"unknown-request": 500})
        with patch.object(self.client, "request", side_effect=[{"input_tokens": 100}, response(["merged\n"])]) as request:
            self.client.resolve("docs/guide.md", CONFLICT)
        self.assertEqual(request.call_args.args[1]["max_output_tokens"], 18144)
        self.assertEqual(self.client.state.data["used_tokens"], 1300)
        self.assertEqual(self.client.state.data["pending"], {"unknown-request": 500})

    def test_model_context_space_also_bounds_output(self):
        with patch.dict(os.environ, OPENAI_MODEL_CONTEXT_WINDOW="1000"), \
                patch.object(self.client, "request", side_effect=[{"input_tokens": 100}, response(["merged\n"])]) as request:
            self.client.resolve("docs/guide.md", CONFLICT)
        self.assertEqual(request.call_args.args[1]["max_output_tokens"], 644)

    def test_unknown_model_requires_explicit_model_specifications(self):
        self.client.model = "custom-model"
        with patch.dict(os.environ, OPENAI_MODEL_CONTEXT_WINDOW="", OPENAI_MODEL_MAX_OUTPUT_TOKENS=""), \
                patch.object(self.client, "request") as request:
            with self.assertRaisesRegex(ValueError, "Unknown model limits"):
                self.client.resolve("docs/guide.md", CONFLICT)
        request.assert_not_called()
        with patch.dict(os.environ, OPENAI_MODEL_CONTEXT_WINDOW="10000", OPENAI_MODEL_MAX_OUTPUT_TOKENS="2500"), \
                patch.object(self.client, "request", side_effect=[{"input_tokens": 100}, response(["merged\n"])]) as request:
            self.client.resolve("docs/guide.md", CONFLICT)
        self.assertEqual(request.call_args.args[1]["max_output_tokens"], 2500)

    def test_old_saved_small_output_allowance_does_not_limit_new_requests(self):
        key, _ = self.client.state.entry(self.client.task, "docs/guide.md", CONFLICT)
        self.client.state.remember(key, self.client.task, max_output_tokens=8192)
        with patch.object(self.client, "request", side_effect=[{"input_tokens": 100}, response(["merged\n"])]) as request:
            self.client.resolve("docs/guide.md", CONFLICT)
        self.assertEqual(request.call_args.args[1]["max_output_tokens"], 19644)

    def test_daily_output_exhaustion_pauses_without_regenerating(self):
        incomplete = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                      "usage": {"total_tokens": 19744}}
        with patch.object(self.client, "request", side_effect=[{"input_tokens": 100}, incomplete]) as request:
            with self.assertRaisesRegex(resolver.BudgetPaused, "remaining daily output allowance"):
                self.client.resolve("docs/guide.md", CONFLICT)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.client.state.data["used_tokens"], 19744)

    def test_model_output_exhaustion_stops_without_doubling_or_retrying(self):
        self.client.limit = 200000
        incomplete = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
                      "usage": {"total_tokens": 128100}}
        with patch.object(self.client, "request", side_effect=[{"input_tokens": 100}, incomplete]) as request:
            with self.assertRaisesRegex(RuntimeError, "model output/context limit"):
                self.client.resolve("docs/guide.md", CONFLICT)
        self.assertEqual(request.call_count, 2)

    def test_input_cannot_exceed_the_model_context(self):
        with patch.dict(os.environ, OPENAI_MODEL_CONTEXT_WINDOW="1000"), \
                patch.object(self.client, "request", return_value={"input_tokens": 1000}) as request:
            with self.assertRaisesRegex(RuntimeError, "Input cannot fit"):
                self.client.resolve("docs/guide.md", CONFLICT)
        self.assertEqual(request.call_count, 1)
        self.assertFalse(self.ledger.exists())

    def test_model_review_request_stops_before_applying_and_is_reused(self):
        answer = response([])
        answer["output"][0]["content"][0]["text"] = json.dumps({
            "resolved": False, "requires_review": True, "reason": "データ消失の可能性", "replacements": []})
        with patch.object(self.client, "request", side_effect=[{"input_tokens": 100}, answer]):
            with self.assertRaisesRegex(RuntimeError, "データ消失"):
                self.client.resolve("app/src/main/java/Migration.kt", CONFLICT)
        with patch.object(self.client, "request") as request:
            with self.assertRaisesRegex(RuntimeError, "データ消失"):
                self.client.resolve("app/src/main/java/Migration.kt", CONFLICT)
            request.assert_not_called()

    def test_whole_update_review_is_budgeted_and_reused_for_identical_diff(self):
        for review in (False, True):
            diff = f"diff --git a/Feature.kt b/Feature.kt\nchange-{review}"
            answer = response([])
            answer["output"][0]["content"][0]["text"] = json.dumps({
                "requires_review": review, "reason": "大規模な設計変更" if review else "通常の修正"})
            with self.subTest(review=review), patch.object(self.client, "request", side_effect=[
                    {"input_tokens": 100}, answer]) as request:
                if review:
                    with self.assertRaisesRegex(RuntimeError, "大規模"):
                        self.client.assess_update(diff)
                else:
                    self.client.assess_update(diff)
                self.assertEqual(request.call_args.args[1]["reasoning"], {"effort": "high"})
            with patch.object(self.client, "request") as request:
                if review:
                    with self.assertRaisesRegex(RuntimeError, "大規模"):
                        self.client.assess_update(diff)
                else:
                    self.client.assess_update(diff)
                request.assert_not_called()
        self.assertEqual(self.client.state.data["used_tokens"], 600)

    def test_whole_update_review_requires_key_and_respects_daily_budget(self):
        self.client.key = ""
        with self.assertRaisesRegex(RuntimeError, "OPENAI_API_KEY"):
            self.client.assess_update("diff")
        self.client.key = "test-only"
        self.client.state.data["used_tokens"] = self.client.limit
        with patch.object(self.client, "request", return_value={"input_tokens": 100}) as request:
            with self.assertRaises(resolver.BudgetPaused):
                self.client.assess_update("diff")
        self.assertEqual(request.call_count, 1)

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
