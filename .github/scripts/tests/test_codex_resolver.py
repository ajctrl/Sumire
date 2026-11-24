import importlib.util
import io
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "codex-resolver.py"
spec = importlib.util.spec_from_file_location("codex_bridge_tests", SCRIPT)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, OPENAI_API_KEY="not-real", SUMIRE_ARTIFACT_CLIENT="",
                         OPENAI_BUDGET_FILE=str(Path(temporary.name) / "repair-state.json"),
                         OPENAI_DAILY_TOKEN_BUDGET="200000")
        env.start()
        self.addCleanup(env.stop)
        self.resolver = bridge.Resolver()
        self.gateway = object.__new__(bridge.Gateway)
        self.gateway.resolver = self.resolver
        self.gateway.token = "local-only"
        self.gateway.failure = None
        self.gateway.progress_key = None

    def answer(self, usage=1000):
        return {"id": "resp_test", "status": "completed", "output": [],
                "usage": {"total_tokens": usage}}

    def test_every_codex_tool_round_is_counted_reserved_and_settled(self):
        payload = {"model": "other-model", "input": "tool result", "max_output_tokens": 999999,
                   "store": True, "reasoning": {"effort": "low"}}
        with patch.object(self.resolver, "request", side_effect=[
                {"input_tokens": 100}, self.answer(), {"input_tokens": 200}, self.answer()]) as send:
            self.gateway.exchange(dict(payload))
            self.gateway.exchange(dict(payload))
        self.assertEqual(self.resolver.state.data["used_tokens"], 2000)
        self.assertEqual(self.resolver.state.data["pending"], {})
        generations = [call.args[1] for call in send.call_args_list if call.args[0] == "responses"]
        for generation in generations:
            self.assertEqual(generation["model"], bridge.transport.DEFAULT_MODEL)
            self.assertEqual(generation["max_output_tokens"], 128000)
            self.assertEqual(generation["reasoning"], {"effort": "high"})
            self.assertFalse(generation["store"])

    def test_daily_exhaustion_blocks_generation_and_unknown_response_keeps_reservation(self):
        with patch.object(self.resolver, "request", side_effect=[
                {"input_tokens": 100}, RuntimeError("connection lost")]):
            with self.assertRaisesRegex(RuntimeError, "connection lost"):
                self.gateway.exchange({"input": "prompt"})
        self.assertEqual(sum(self.resolver.state.data["pending"].values()), 128356)
        self.resolver.state.data["used_tokens"] = 200000
        with patch.object(self.resolver, "request", return_value={"input_tokens": 100}) as send:
            with self.assertRaises(bridge.BudgetPaused):
                self.gateway.exchange({"input": "prompt"})
            self.assertEqual(send.call_count, 1)

    def test_investigation_is_saved_before_the_next_codex_round(self):
        key, _ = self.resolver.state.entry(self.resolver.task, "codex-session", "request")
        self.gateway.progress_key = key
        transcript = [{"type": "function_call_output", "call_id": "read1", "output": "source context"}]
        with patch.object(self.resolver, "request", side_effect=[{"input_tokens": 100}, self.answer()]):
            self.gateway.exchange({"input": transcript})
        saved = json.loads(self.resolver.state.file.read_text())
        self.assertEqual(saved["repairs"][key]["transcript"], transcript)

    def test_next_model_round_keeps_previously_saved_partial_edits(self):
        key, _ = self.resolver.state.entry(self.resolver.task, "codex-session", "request")
        changed = {"Feature.kt": {"text": "partial edit", "executable": False}}
        self.resolver.state.remember(key, self.resolver.task, edits=changed)
        self.gateway.progress_key = key
        with patch.object(self.resolver, "request", side_effect=[{"input_tokens": 100}, self.answer()]):
            self.gateway.exchange({"input": "next tool round"})
        self.assertEqual(self.resolver.state.data["repairs"][key]["edits"], changed)

    def test_worker_has_only_gateway_token_and_a_copy_without_git_or_project_config(self):
        old = Path.cwd()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source"
            source.mkdir()
            (source / "Feature.kt").write_text("source context")
            for name in (".git", ".codex"):
                (source / name).mkdir()
                (source / name / "config").write_text("private configuration")
            worker = Mock(failure=None, token="gateway-only", server_port=12345)
            payload = {"instructions": "Review source", "input": "Feature.kt",
                       "text": {"format": {"schema": {"type": "object"}}}}

            def run(command, **kwargs):
                if command[:2] in (["sudo", "chown"], ["sudo", "rm"]):
                    return Mock(returncode=0)
                self.assertEqual(command[:4], ["sudo", "-u", "sumire-codex", "env"])
                self.assertIn("-i", command)
                self.assertNotIn("not-real", " ".join(command))
                self.assertNotIn("private-github-token", " ".join(command))
                copy = kwargs["cwd"]
                self.assertEqual((copy / "Feature.kt").read_text(), "source context")
                self.assertFalse((copy / ".git").exists())
                self.assertFalse((copy / ".codex").exists())
                self.assertIn("--sandbox", command)
                self.assertIn("read-only", command)
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text('{"requires_review": false}')
                return Mock(returncode=0)

            try:
                os.chdir(source)
                with patch.dict(os.environ, SUMIRE_CODEX_BIN=str(SCRIPT), GH_TOKEN="private-github-token"), \
                        patch.object(bridge, "Gateway", return_value=worker), \
                        patch.object(bridge.pwd, "getpwnam", return_value=Mock(pw_uid=65534, pw_gid=65534)), \
                        patch.object(bridge.subprocess, "run", side_effect=run):
                    self.assertEqual(self.resolver.complete(payload, "Feature.kt", 1000000, 128000),
                                     {"requires_review": False})
            finally:
                os.chdir(old)

    def test_real_worker_uid_can_start_write_results_and_cleanup_on_all_outcomes(self):
        if os.getuid() == 0 or not shutil.which("sudo"):
            self.skipTest("Requires a non-root parent and passwordless sudo")
        probe = subprocess.run(["sudo", "-n", "-u", "nobody", "true"], capture_output=True)
        if probe.returncode:
            self.skipTest("Passwordless sudo is unavailable")
        original_directory = Path.cwd()
        real_temporary_directory = tempfile.TemporaryDirectory
        worker_uid = pwd.getpwnam("nobody").pw_uid
        with real_temporary_directory(prefix="sumire-worker-test-") as fixture:
            fixture = Path(fixture)
            fixture.chmod(0o755)
            source = fixture / "source"
            source.mkdir(mode=0o700)
            (source / "Feature.kt").write_text("source context")
            (source / "Feature.kt").chmod(0o600)
            fake = fixture / "codex"
            fake.write_text("#!/usr/bin/python3\n" +
                "import json, os, pathlib, sys\n" +
                "assert os.getuid() == " + str(worker_uid) + "\n" +
                "assert 'OPENAI_API_KEY' not in os.environ and 'GH_TOKEN' not in os.environ\n" +
                "editable = sys.argv[sys.argv.index('--sandbox') + 1] == 'workspace-write'\n" +
                "assert pathlib.Path('Feature.kt').read_text() in (('source context', 'direct edit') if editable else ('source context',))\n" +
                "assert os.access('Feature.kt', os.W_OK) == editable\n" +
                "if editable:\n" +
                "    replacement = pathlib.Path('replacement.tmp')\n" +
                "    replacement.write_text('direct edit'); replacement.chmod(0o600)\n" +
                "    replacement.replace('Feature.kt')\n" +
                "    pathlib.Path('related').mkdir(mode=0o700, exist_ok=True)\n" +
                "    pathlib.Path('related', 'New.kt').write_text('related change')\n" +
                "assert pathlib.Path(os.environ['CODEX_HOME'], 'config.toml').is_file()\n" +
                "assert pathlib.Path(sys.argv[sys.argv.index('--output-schema') + 1]).is_file()\n" +
                "pathlib.Path(os.environ['HOME'], 'nested').mkdir()\n" +
                "pathlib.Path(os.environ['HOME'], 'nested', 'private').write_text('worker state')\n" +
                "output = pathlib.Path(sys.argv[sys.argv.index('--output-last-message') + 1])\n" +
                "assert output.stat().st_uid != os.getuid()\n" +
                "output.write_text(json.dumps({'requires_review': False}))\n" +
                "sys.exit(23 if sys.stdin.read().endswith('fail') else 0)\n")
            fake.chmod(0o755)
            payload = {"instructions": "Read source", "input": "Feature.kt",
                       "text": {"format": {"schema": {"type": "object"}}}}
            try:
                os.chdir(source)
                for outcome in ("success", "failure", "budget_pause", "editable", "editable_pause"):
                    with self.subTest(outcome=outcome):
                        directories = []

                        def temporary_directory(*args, **kwargs):
                            temporary = real_temporary_directory(*args, **kwargs)
                            directories.append(Path(temporary.name))
                            return temporary

                        gateway = Mock(token="gateway-only", server_port=12345,
                                       failure=bridge.BudgetPaused("Daily allowance reached")
                                       if outcome in ("budget_pause", "editable_pause") else None)
                        request = dict(payload, input="fail" if outcome == "failure" else "Feature.kt")
                        with patch.dict(os.environ, SUMIRE_CODEX_BIN=str(fake), SUMIRE_CODEX_USER="nobody"), \
                                patch.object(bridge, "Gateway", return_value=gateway), \
                                patch.object(bridge.tempfile, "TemporaryDirectory", side_effect=temporary_directory):
                            if outcome == "success":
                                self.assertEqual(self.resolver.complete(request, "Feature.kt", 1000000, 128000),
                                                 {"requires_review": False})
                            elif outcome in ("editable", "editable_pause"):
                                if outcome == "editable_pause":
                                    with self.assertRaises(bridge.BudgetPaused):
                                        self.resolver.complete(request, "Feature.kt", 1000000, 128000, editable=True)
                                    progress = [entry for entry in self.resolver.state.data["repairs"].values() if "edits" in entry]
                                    self.assertEqual(progress[-1]["edits"]["Feature.kt"]["text"], "direct edit")
                                else:
                                    result = self.resolver.complete(request, "Feature.kt", 1000000, 128000, editable=True)
                                    self.assertEqual(result["edits"]["Feature.kt"]["text"], "direct edit")
                                    self.assertEqual((source / "Feature.kt").read_text(), "source context")
                            elif outcome == "budget_pause":
                                with self.assertRaises(bridge.BudgetPaused):
                                    self.resolver.complete(request, "Feature.kt", 1000000, 128000)
                            else:
                                with self.assertRaisesRegex(RuntimeError, "Codex did not finish"):
                                    self.resolver.complete(request, "Feature.kt", 1000000, 128000)
                        self.assertEqual(len(directories), 1)
                        self.assertFalse(directories[0].exists())
            finally:
                os.chdir(original_directory)

    def test_direct_edits_are_applied_and_cached_but_human_review_edits_are_not(self):
        old = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                Path("Feature.kt").write_text("<<<<<<< ours\nold\n=======\nnew\n>>>>>>> theirs\n")
                conflict = [("Feature.kt", Path("Feature.kt").read_text())]
                changed = {"Feature.kt": {"text": "merged code\n", "executable": False},
                           "related.kt": {"text": "related adjustment\n", "executable": False}}
                with patch.object(self.resolver, "complete", return_value={
                        "completed": True, "requires_review": False, "reason": "通常の更新", "edits": changed}):
                    self.resolver.repair_update(conflict)
                self.assertEqual(Path("related.kt").read_text(), "related adjustment\n")
                Path("Feature.kt").write_text(conflict[0][1])
                with patch.object(self.resolver, "complete") as complete:
                    self.resolver.repair_update(conflict)
                    complete.assert_not_called()
                self.assertEqual(Path("Feature.kt").read_text(), "merged code\n")
                other = bridge.Resolver(task="different-update")
                with patch.object(other, "complete", return_value={
                        "completed": False, "requires_review": True, "reason": "互換性の破壊", "edits": changed}):
                    with self.assertRaisesRegex(RuntimeError, "互換性"):
                        other.repair_update(conflict)
                self.assertEqual(Path("Feature.kt").read_text(), "merged code\n")
            finally:
                os.chdir(old)

    def handler(self, authenticated=True):
        handler = object.__new__(bridge.Handler)
        handler.path = "/v1/responses"
        handler.headers = {"Content-Length": "2", "Authorization": "Bearer local-only" if authenticated else "Bearer wrong"}
        handler.rfile = io.BytesIO(b"{}")
        handler.wfile = io.BytesIO()
        handler.server = self.gateway
        handler.send_error = Mock()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        return handler

    def test_sse_final_event_is_emitted_only_after_usage_was_saved(self):
        with patch.object(self.resolver, "request", side_effect=[{"input_tokens": 100}, self.answer()]):
            handler = self.handler()
            handler.do_POST()
        self.assertIn(b"event: response.completed", handler.wfile.getvalue())
        persisted = json.loads(self.resolver.state.file.read_text())
        self.assertEqual(persisted["used_tokens"], 1000)
        self.assertEqual(persisted["pending"], {})

    def test_unauthorized_request_never_reaches_api(self):
        with patch.object(self.resolver, "request") as send:
            handler = self.handler(authenticated=False)
            handler.do_POST()
            handler.send_error.assert_called_once_with(403)
            send.assert_not_called()

    def test_gateway_failure_is_latched_across_codex_retries(self):
        with patch.object(self.resolver, "request", side_effect=RuntimeError("unavailable")) as send:
            first = self.handler()
            first.do_POST()
            second = self.handler()
            second.do_POST()
            self.assertEqual(send.call_count, 1)
            self.assertIsInstance(self.gateway.failure, RuntimeError)
            second.send_response.assert_called_once_with(502)


if __name__ == "__main__":
    unittest.main()
