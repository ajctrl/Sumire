import copy
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

import test_sync_upstream as fixtures

sync = fixtures.sync
repair = sync.validation_repair


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.repo = "ajctrl/Sumire"
        common = dict(status="completed", conclusion="success", head_branch="dev", head_sha="a" * 40,
                      repository={"full_name": self.repo}, head_repository={"full_name": self.repo}, run_attempt=1)
        self.run = dict(common, event="workflow_run", path=".github/workflows/sync-upstream.yml",
                        conclusion="failure", display_title="Validate upstream 10:1")
        self.source = dict(common, event="workflow_dispatch", path=".github/workflows/prepare-upstream-sync.yml")
        self.plan = dict(version=2, repository=self.repo, run_id=10, run_attempt=1, changed=True,
                         paused=False, review_required=False, full_validation=True,
                         base="a" * 40, head="b" * 40, pr=6)
        self.jobs = [{"name": "prepare", "conclusion": "success"},
                     {"id": 3, "name": "validate / Full build, tests and lint", "conclusion": "failure", "html_url": "job-url"}]

    def api(self, path):
        if path.endswith("runs/20"):
            return self.run
        if path.endswith("runs/10/attempts/1"):
            return self.source
        if "/runs/10/artifacts?" in path:
            return {"artifacts": [{"name": "sumire-sync-plan-10-1", "id": 7, "expired": False}]}
        if "/runs/20/attempts/1/jobs?" in path:
            return {"jobs": self.jobs}
        self.fail(path)

    def raw(self, path):
        if path.endswith("artifacts/7/zip"):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                archive.writestr("sync-plan.json", json.dumps(self.plan))
            return stream.getvalue()
        if path.endswith("jobs/3/logs"):
            return b"test failure: unexpected state"
        self.fail(path)

    def load(self):
        with patch.object(repair, "api", side_effect=self.api), patch.object(repair, "raw", side_effect=self.raw):
            return repair.failure(self.repo, "20")

    def test_only_trusted_preparation_supplies_candidate(self):
        plan, logs = self.load()
        self.assertEqual(plan, self.plan)
        self.assertEqual(logs[0]["log"], "test failure: unexpected state")

    def test_rejects_foreign_runs_wrong_workflows_and_artifact_identity(self):
        for target, key, value in ((self.run, "event", "pull_request"),
                                   (self.run, "head_branch", "feature"),
                                   (self.run, "display_title", "arbitrary untrusted title"),
                                   (self.source, "path", ".github/workflows/fork-validation.yml"),
                                   (self.source, "head_repository", {"full_name": "attacker/fork"}),
                                   (self.plan, "run_attempt", 2), (self.plan, "repository", "attacker/fork")):
            old = copy.deepcopy(target[key])
            with self.subTest(key=key, value=value):
                target[key] = value
                with self.assertRaises(ValueError):
                    self.load()
                target[key] = old

    def test_success_cancelled_stale_base_and_nonvalidation_failures_do_not_repair(self):
        for conclusion in ("success", "cancelled"):
            self.run["conclusion"] = conclusion
            self.assertIsNone(self.load())
        self.run["conclusion"] = "failure"
        self.run["head_sha"] = "c" * 40
        self.assertIsNone(self.load())
        self.run["head_sha"] = "a" * 40
        for name in ("merge", "release / publish", "prepare"):
            self.jobs[1]["name"] = name
            self.assertIsNone(self.load())


class RepairIntegrationTests(unittest.TestCase):
    # Reuse the local fork/upstream/origin fixture, not its test cases.
    setUp = fixtures.MergeTests.setUp
    git = fixtures.MergeTests.git
    identity = fixtures.MergeTests.identity
    commit = fixtures.MergeTests.commit
    fake_run = fixtures.MergeTests.fake_run
    invoke = fixtures.MergeTests.invoke

    def candidate(self):
        with patch.dict(os.environ, OPENAI_BUDGET_FILE=str(self.root / "ledger")):
            self.invoke()
        self.head = self.git("rev-parse", "HEAD")
        self.branch = self.git("branch", "--show-current")
        self.next_state = json.loads(Path(".github/upstream.json").read_text())
        self.git("switch", "dev")
        self.plan = dict(base=self.base, head=self.head, pr=42)
        self.pull = dict(state="open", draft=False, base={"ref": "dev"}, head={
            "sha": self.head, "ref": self.branch, "repo": {"full_name": "ajctrl/Sumire"}})
        (self.root / "outputs").unlink()

    def resume(self, edits=None, error=None, after_api=None):
        def response(*args, **kwargs):
            if error:
                raise error
            return {"completed": True, "requires_review": False, "reason": "diagnosed failure", "edits": edits or {}}
        def api(path):
            if path.endswith("/git/ref/heads/dev"):
                return {"object": {"sha": self.base}}
            return self.pull
        env = dict(OPENAI_API_KEY="test-only", OPENAI_BUDGET_FILE=str(self.root / "ledger"),
                   SUMIRE_FAILED_VALIDATION_RUN="20")
        with patch.dict(os.environ, **env), \
                patch.object(repair, "failure", return_value=(self.plan, [{"log": "test failed", "url": "job"}])) , \
                patch.object(sync, "api", side_effect=after_api or api), \
                patch.object(sync, "run", side_effect=self.fake_run), \
                patch.object(sync.conflict_resolver.Resolver, "complete", side_effect=response) as complete:
            sync.main()
        return complete

    def test_clean_merge_failure_is_repaired_then_all_validation_is_requested(self):
        self.candidate()
        calls = self.resume({"fork.txt": {"text": "fixed integration\n", "executable": False}})
        repaired = self.git("rev-parse", "HEAD")
        self.assertNotEqual(repaired, self.head)
        self.assertEqual(self.git("show", "-s", "--format=%P", repaired), self.head)
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/{self.branch}").split()[0], repaired)
        self.assertEqual(repair.candidate_depth(sync, self.base, self.target, repaired, self.next_state), 1)
        self.assertIn("full_validation=true", (self.root / "outputs").read_text())
        description = json.loads(calls.call_args.args[0]["input"])
        self.assertEqual(description["conflicts"], [])
        self.assertEqual(description["whole_update"]["candidate"], self.head)
        self.git("switch", "dev")
        self.git("branch", "-D", self.branch)
        with patch.dict(os.environ, OPENAI_BUDGET_FILE=str(self.root / "ledger")):
            self.invoke([{"number": 42, "state": "open"}])
        self.assertEqual(self.git("rev-parse", "HEAD"), repaired)

    def test_stale_candidate_never_calls_codex_or_pushes(self):
        self.candidate()
        self.pull["head"]["sha"] = "c" * 40
        calls = self.resume()
        calls.assert_not_called()
        self.assertIn("paused=true", (self.root / "outputs").read_text())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_stale_dev_never_calls_codex(self):
        self.candidate()
        self.plan["base"] = "c" * 40
        self.resume().assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_candidate_change_during_repair_prevents_push(self):
        self.candidate()
        changed = copy.deepcopy(self.pull)
        changed["head"]["sha"] = "c" * 40
        with self.assertRaisesRegex(RuntimeError, "changed during repair"):
            self.resume({"fork.txt": {"text": "fix\n", "executable": False}},
                        after_api=iter([self.pull, changed, {"object": {"sha": self.base}}]))
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/{self.branch}").split()[0], self.head)

    def test_release_version_changes_are_handed_off(self):
        self.candidate()
        self.resume({"app/build.gradle": {"text": 'versionName "1.7.129"\nversionCode 999\n', "executable": False}})
        self.record_review.assert_called_once()
        self.assertIn("release version numbers", self.record_review.call_args.args[4])
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/{self.branch}").split()[0], self.head)

    def test_budget_pause_records_candidate_and_scheduled_run_resumes_it(self):
        self.candidate()
        self.resume(error=sync.conflict_resolver.BudgetPaused("daily allowance"))
        state = json.loads((self.root / "ledger").read_text())
        self.assertEqual(state["repairs"][repair.pending_key(self.base, self.head)]["failed_run"], "20")
        self.assertIn("paused=true", (self.root / "outputs").read_text())
        self.git("switch", "dev")
        with patch.dict(os.environ, OPENAI_BUDGET_FILE=str(self.root / "ledger")), \
                patch.object(repair, "resume") as resume:
            self.invoke()
        resume.assert_called_once()
        self.assertEqual(resume.call_args.args[1], "20")

    def test_no_progress_hands_off_instead_of_looping(self):
        self.candidate()
        self.resume()
        self.record_review.assert_called_once()
        self.assertIn("Repair made no changes", self.record_review.call_args.args[4])
        self.assertIn("review_required=true", (self.root / "outputs").read_text())

    def test_worker_failure_hands_off_with_validation_link(self):
        self.candidate()
        self.resume(error=RuntimeError("Codex changed a protected path"))
        self.record_review.assert_called_once()
        reason = self.record_review.call_args.args[4]
        self.assertIn("protected path", reason)
        self.assertIn("/actions/runs/20", reason)

    def test_more_than_two_repairs_continue_but_repeated_candidate_is_handed_off(self):
        self.candidate()
        for count in range(1, 6):
            self.resume({"fork.txt": {"text": f"fix {count}\n", "executable": False}})
            self.head = self.git("rev-parse", "HEAD")
            self.plan["head"] = self.head
            self.pull["head"]["sha"] = self.head
            self.git("switch", "dev")
            self.record_review.assert_not_called()
        self.assertEqual(repair.candidate_depth(sync, self.base, self.target, self.head, self.next_state), 5)
        calls = self.resume({"fork.txt": {"text": "fix 1\n", "executable": False}})
        calls.assert_called_once()
        self.record_review.assert_called_once()
        self.assertIn("previous candidate tree", self.record_review.call_args.args[4])
        self.assertEqual(self.git("ls-remote", "origin", f"refs/heads/{self.branch}").split()[0], self.head)
        self.git("reset", "--hard", self.head)
        self.git("switch", "dev")
        self.pending_review.return_value = {"html_url": "review-url", "body": "review pending"}
        calls = self.resume()
        calls.assert_not_called()
        self.assertEqual(self.record_review.call_count, 1)

    def test_hand_modified_candidate_cannot_be_reused(self):
        self.candidate()
        self.git("switch", "--detach", self.head)
        Path("fork.txt").write_text("unreviewed modification")
        self.commit("manual edit")
        modified = self.git("rev-parse", "HEAD")
        with self.assertRaises(sync.conflict_resolver.policy.ReviewRequired):
            repair.candidate_depth(sync, self.base, self.target, modified, self.next_state)


if __name__ == "__main__":
    unittest.main()
