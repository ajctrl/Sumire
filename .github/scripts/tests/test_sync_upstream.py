import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[1] / "sync-upstream.py"
spec = importlib.util.spec_from_file_location("sync_upstream", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class ReleaseTests(unittest.TestCase):
    def test_numeric_order_and_no_downgrade(self):
        release = dict(tag_name="v1.7.128", draft=False, prerelease=False)
        self.assertEqual(sync.release_tag(release, "v1.7.99"), "v1.7.128")
        self.assertIsNone(sync.release_tag(release, "v1.7.128"))
        self.assertIsNone(sync.release_tag(release, "v1.8.0"))

    def test_reject_unstable_or_invalid_tags(self):
        for tag in ("v1.7.129-rc1", "main", "--help", "v1.7.129\n"):
            with self.assertRaises(ValueError):
                sync.version(tag)
        for key in ("draft", "prerelease"):
            release = dict(tag_name="v1.7.129", draft=False, prerelease=False)
            release[key] = True
            with self.assertRaises(ValueError):
                sync.release_tag(release, "v1.7.128")


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old_cwd = Path.cwd()
        self.addCleanup(os.chdir, self.old_cwd)
        os.chdir(self.root)
        self.git("init", "-b", "dev", "upstream")
        os.chdir(self.root / "upstream")
        self.identity()
        Path("docs").mkdir()
        Path("docs/guide.md").write_text("base\n")
        Path("app").mkdir()
        Path("app/build.gradle").write_text(
            'android {\n    versionName "1.7.128"\n    versionCode 821\n}\n')
        self.commit("initial")
        self.initial = self.git("rev-parse", "HEAD")
        self.git("clone", "--bare", ".", str(self.root / "origin.git"))
        self.git("clone", str(self.root / "origin.git"), str(self.root / "fork"))
        Path("docs/guide.md").write_text("upstream update\n")
        Path("app/build.gradle").write_text(
            'android {\n    versionName "1.7.129"\n    versionCode 822\n}\n')
        self.commit("release")
        self.target = self.git("rev-parse", "HEAD")
        self.git("tag", "v1.7.129")
        os.chdir(self.root / "fork")
        self.identity()
        Path(".github").mkdir()
        Path("fork.txt").write_text("keep me\n")
        Path(".github/upstream.json").write_text(json.dumps({
            "repository": sync.UPSTREAM, "tag": "v1.7.128", "commit": self.initial}))
        self.commit("fork")
        self.git("push", "origin", "dev")
        self.base = self.git("rev-parse", "HEAD")
        self.env = patch.dict(os.environ, GITHUB_REPOSITORY="ajctrl/Sumire",
                              GITHUB_OUTPUT=str(self.root / "outputs"), OPENAI_API_KEY="",
                              RUNNER_TEMP=str(self.root / "runner-temp"))
        self.env.start()
        self.addCleanup(self.env.stop)
        # Exercise real transport/ledger logic with mocked OpenAI responses;
        # Codex subprocess/gateway isolation has separate tests.
        complete = patch.object(sync.conflict_resolver.Resolver, "complete",
                                sync.conflict_resolver.transport.Resolver.complete)
        complete.start()
        self.addCleanup(complete.stop)
        pending = patch.object(sync.upstream_review, "find", return_value=None)
        self.pending_review = pending.start()
        self.addCleanup(pending.stop)
        record = patch.object(sync.upstream_review, "record", return_value={"html_url": "https://github.com/ajctrl/Sumire/issues/43"})
        self.record_review = record.start()
        self.addCleanup(record.stop)
        self.real_run = sync.run
        risk = patch.object(sync.conflict_resolver.Resolver, "assess_update")
        risk.start()
        self.addCleanup(risk.stop)

    def git(self, *args):
        return subprocess.run(["git", *args], check=True, text=True,
                              capture_output=True).stdout.strip()

    def identity(self):
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)

    def fake_run(self, *args, **kwargs):
        args = list(args)
        if args[:2] == ["git", "fetch"]:
            args = [str(self.root / "upstream") if arg ==
                    f"https://github.com/{sync.UPSTREAM}.git" else arg for arg in args]
        if args[:3] == ["gh", "pr", "create"] or args[0] == "python3":
            return subprocess.CompletedProcess(args, 0, "", "")
        return self.real_run(*args, **kwargs)

    def invoke(self, pulls=None):
        release = dict(tag_name="v1.7.129", draft=False, prerelease=False)
        replies = [release, pulls or [], [{"number": 42}]]
        with patch.object(sync, "api", side_effect=replies), \
                patch.object(sync, "run", side_effect=self.fake_run):
            sync.main()

    def prepare_document_conflicts(self, count):
        """Create real three-stage conflicts sharing a common document baseline."""
        paths = [Path("docs/guide.md")] + [Path(f"docs/guide-{i}.md") for i in range(count - 1)]
        os.chdir(self.root / "upstream")
        self.git("switch", "-c", "expanded-base", self.initial)
        for path in paths[1:]:
            path.write_text("base prose\n")
        self.commit("shared documentation baseline")
        common = self.git("rev-parse", "HEAD")
        for path in paths:
            path.write_text("upstream prose\n")
        Path("app/build.gradle").write_text(
            'android {\n    versionName "1.7.129"\n    versionCode 822\n}\n')
        self.commit("release documentation update")
        self.target = self.git("rev-parse", "HEAD")
        self.git("tag", "-f", "v1.7.129")
        os.chdir(self.root / "fork")
        self.git("fetch", str(self.root / "upstream"), common)
        self.git("merge", "--no-edit", common)
        for path in paths:
            path.write_text("fork prose\n")
        self.commit("fork documentation changes")
        self.git("push", "origin", "dev")
        self.base = self.git("rev-parse", "HEAD")
        return paths

    def test_direct_codex_edit_resolves_many_files_and_related_code(self):
        paths = self.prepare_document_conflicts(9)
        changed = {str(path): {"text": "combined prose\n", "executable": False} for path in paths}
        changed["related.kt"] = {"text": "related adjustment\n", "executable": False}
        with patch.dict(os.environ, OPENAI_API_KEY="test-only", OPENAI_BUDGET_FILE=str(self.root / "ledger")), \
                patch.object(sync.conflict_resolver.Resolver, "complete", return_value={
                    "completed": True, "requires_review": False, "reason": "通常の更新", "edits": changed}) as complete:
            self.invoke()
        self.assertTrue(complete.call_args.kwargs["editable"])
        description = json.loads(complete.call_args.args[0]["input"])
        self.assertIn("app/build.gradle", description["whole_update"]["paths"])
        self.assertIn("upstream prose", description["whole_update"]["diff"])
        self.assertEqual(self.git("show", "HEAD:related.kt"), "related adjustment")
        for path in paths:
            self.assertEqual(path.read_text(), "combined prose\n")

    def test_merge_preserves_fork_and_ancestry_and_retry(self):
        self.invoke()
        first = self.git("rev-parse", "HEAD")
        self.assertEqual(Path("fork.txt").read_text(), "keep me\n")
        self.assertEqual(Path("docs/guide.md").read_text(), "upstream update\n")
        self.assertEqual(self.git("show", "-s", "--format=%P", "HEAD"),
                         f"{self.base} {self.target}")
        self.assertEqual(json.loads(Path(".github/upstream.json").read_text())["commit"],
                         self.target)
        branch = self.git("branch", "--show-current")
        self.git("switch", "dev")
        self.git("branch", "-D", branch)
        self.invoke([{"number": 42, "state": "open"}])
        self.assertIn(f"head={first}\n", (self.root / "outputs").read_text())

    def test_conflict_stops_without_changing_dev(self):
        Path("docs/guide.md").write_text("fork conflicting change\n")
        self.commit("conflict")
        self.git("push", "origin", "dev")
        base = self.git("rev-parse", "HEAD")
        with self.assertRaisesRegex(RuntimeError, "Manual upstream merge required"):
            self.invoke()
        self.assertEqual(self.git("rev-parse", "HEAD"), base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.root / "outputs").exists())

    def test_next_upstream_after_manual_revision_preserves_changes_and_increases_code(self):
        Path("app/build.gradle").write_text(
            'android {\n    versionName "1.7.128.2"\n    versionCode 823\n}\n'
            '// fork configuration\n')
        self.commit("manual revision")
        self.git("push", "origin", "dev")
        base = self.git("rev-parse", "HEAD")
        with patch.object(sync.conflict_resolver, "Resolver") as resolver:
            self.invoke()
        resolver.assert_not_called()
        gradle = Path("app/build.gradle").read_text()
        self.assertEqual(sync.release_versions.version_info(gradle), ("1.7.129", 824))
        self.assertIn('// fork configuration', gradle)
        self.assertEqual(self.git("show", "-s", "--format=%P", "HEAD"),
                         f"{base} {self.target}")
        self.assertEqual(json.loads(Path('.github/upstream.json').read_text())['tag'], 'v1.7.129')

    def test_gradle_non_version_conflict_is_not_overwritten(self):
        os.chdir(self.root / 'upstream')
        p = Path('app/build.gradle')
        p.write_text(p.read_text().replace('android {', 'android { // upstream'))
        self.commit('upstream configuration')
        self.git('tag', '-f', 'v1.7.129')
        os.chdir(self.root / 'fork')
        p.write_text(p.read_text().replace('android {', 'android { // fork'))
        self.commit('fork configuration')
        self.git('push', 'origin', 'dev')
        before = self.git('rev-parse', 'HEAD')
        with self.assertRaisesRegex(RuntimeError, 'app/build.gradle'):
            self.invoke()
        self.assertEqual(self.git('rev-parse', 'HEAD'), before)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_changed_automation_requires_manual_review(self):
        os.chdir(self.root / "upstream")
        Path(".github/workflows").mkdir(parents=True)
        Path(".github/workflows/sync-upstream.yml").write_text("changed\n")
        self.commit("alter automation")
        self.git("tag", "-f", "v1.7.129")
        os.chdir(self.root / "fork")
        self.invoke()
        self.assertTrue(self.record_review.called)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_api_resolves_allowed_documentation_and_retry_reuses_exact_candidate(self):
        Path("docs/guide.md").write_text("fork change\n")
        self.commit("fork change")
        self.git("push", "origin", "dev")
        base = self.git("rev-parse", "HEAD")
        client = Mock(enabled=True)
        client.state.data = {"repairs": {}}
        def repair(conflicts, overview=None):
            for path, _ in conflicts:
                Path(path).write_text("fork and upstream combined\n")
            return [path for path, _ in conflicts]
        client.repair_update.side_effect = repair
        with patch.object(sync.conflict_resolver, "Resolver", return_value=client):
            self.invoke()
            first = self.git("rev-parse", "HEAD")
            branch = self.git("branch", "--show-current")
            self.assertEqual(Path("docs/guide.md").read_text(), "fork and upstream combined\n")
            self.assertEqual(self.git("show", "-s", "--format=%P", first),
                             f"{base} {self.target}")
            self.git("switch", "dev")
            self.git("branch", "-D", branch)
            self.invoke([{"number": 42, "state": "open"}])
        client.repair_update.assert_called_once()
        self.assertEqual(self.git("rev-parse", "HEAD"), first)

    def test_api_failure_aborts_merge_without_pushing_a_candidate(self):
        Path("docs/guide.md").write_text("fork conflict\n")
        self.commit("fork change")
        self.git("push", "origin", "dev")
        before = self.git("rev-parse", "HEAD")
        client = Mock(enabled=True)
        client.state.data = {"repairs": {}}
        client.repair_update.side_effect = RuntimeError("budget exhausted")
        with patch.object(sync.conflict_resolver, "Resolver", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
                self.invoke()
        self.assertEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse((self.root / "outputs").exists())
        self.assertEqual(self.git("ls-remote", "--heads", "origin", "refs/heads/upstream/*"), "")

    def test_api_mode_stops_on_automation_changes_without_calling_model(self):
        os.chdir(self.root / "upstream")
        Path(".github/workflows").mkdir(parents=True)
        Path(".github/workflows/android.yml").write_text("upstream release policy\n")
        self.commit("change release workflow")
        self.git("tag", "-f", "v1.7.129")
        os.chdir(self.root / "fork")
        client = Mock(enabled=True)
        client.state.data = {"repairs": {}}
        with patch.object(sync.conflict_resolver, "Resolver", return_value=client):
            self.invoke()
            self.assertTrue(self.record_review.called)
        client.resolve.assert_not_called()
        self.assertFalse(Path(".github/workflows/android.yml").exists())

    def test_api_repairs_gradle_configuration_and_preserves_release_numbers(self):
        os.chdir(self.root / "upstream")
        gradle = Path("app/build.gradle")
        gradle.write_text(gradle.read_text().replace("android {", "android { // upstream"))
        self.commit("upstream configuration")
        self.git("tag", "-f", "v1.7.129")
        os.chdir(self.root / "fork")
        gradle.write_text('android { // fork\n    versionName "1.7.128.2"\n'
                          '    versionCode 830\n}\n')
        self.commit("fork configuration and manual version")
        self.git("push", "origin", "dev")
        client = Mock(enabled=True)
        client.state.data = {"repairs": {}}
        def repair(conflicts, overview=None):
            gradle.write_text('android { // combined\n    versionName "1.7.128"\n    versionCode 821\n}\n')
            return [str(gradle)]
        client.repair_update.side_effect = repair
        with patch.object(sync.conflict_resolver, "Resolver", return_value=client):
            self.invoke()
        client.repair_update.assert_called_once()
        client.assess_update.assert_called_once()
        self.assertEqual(sync.release_versions.version_info(gradle.read_text()), ("1.7.129", 831))

    def record_sibling_release(self, change_release_tree=False):
        self.git("switch", "-c", "previous-release", self.initial)
        if change_release_tree:
            Path("docs/guide.md").write_text("previous release guide\n")
            Path("obsolete.txt").write_text("previous release only\n")
            with Path(sync.VERSION_FILE).open("a") as gradle:
                gradle.write("// previous release only\n")
            self.commit("release-only changes")
        else:
            self.git("commit", "--allow-empty", "-m", "tag previous release snapshot")
        previous = self.git("rev-parse", "HEAD")
        self.git("switch", "dev")
        self.git("merge", "--no-edit", previous)
        state = json.loads(sync.STATE.read_text())
        sync.STATE.write_text(json.dumps({**state, "commit": previous}))
        self.commit("record previous release")
        self.git("push", "origin", "dev")
        return previous

    def test_new_release_after_equivalent_sibling_snapshot(self):
        self.record_sibling_release()
        base = self.git("rev-parse", "HEAD")
        self.invoke()
        self.assertEqual(self.git("show", "-s", "--format=%P", "HEAD"), f"{base} {self.target}")
        self.assertEqual(Path("docs/guide.md").read_text(), "upstream update\n")
        self.assertEqual(Path("fork.txt").read_text(), "keep me\n")
        self.assertIn("changed=true", (self.root / "outputs").read_text())

    def test_divergent_release_uses_recorded_tree_and_sends_real_conflict_to_codex(self):
        previous = self.record_sibling_release(change_release_tree=True)
        Path("docs/guide.md").write_text("fork guide\n")
        self.commit("customize fork guide")
        self.git("push", "origin", "dev")
        base = self.git("rev-parse", "HEAD")
        client = Mock(enabled=True)
        def repair(conflicts, overview):
            self.assertEqual([path for path, _ in conflicts], ["docs/guide.md"])
            self.assertEqual(self.git("show", ":1:docs/guide.md"), self.git("show", f"{previous}:docs/guide.md"))
            self.assertIn("previous release guide", conflicts[0][1])
            Path("docs/guide.md").write_text("combined guide\n")
            return ["docs/guide.md"]
        client.repair_update.side_effect = repair
        with patch.object(sync.conflict_resolver, "Resolver", return_value=client):
            self.invoke()
        client.repair_update.assert_called_once()
        client.assess_update.assert_called_once()
        self.assertEqual(self.git("show", "-s", "--format=%P", "HEAD"), f"{base} {self.target}")
        self.assertFalse(Path("obsolete.txt").exists())
        self.assertNotIn("previous release only", Path(sync.VERSION_FILE).read_text())
        self.assertEqual(json.loads(sync.STATE.read_text())["commit"], self.target)

    def test_older_release_commit_requires_review(self):
        self.git("fetch", str(self.root / "upstream"), "dev")
        with self.assertRaisesRegex(RuntimeError, "already in dev history"):
            sync.release_merge_base(self.target, self.initial, self.target)

    def test_modified_candidate_is_rejected_before_reuse(self):
        self.invoke()
        branch = self.git("branch", "--show-current")
        Path("docs/guide.md").write_text("changed outside automation\n")
        self.git("add", ".")
        self.git("commit", "--amend", "--no-edit")
        self.git("push", "--force", "origin", branch)  # Isolated local test remote.
        self.git("switch", "dev")
        self.git("branch", "-D", branch)
        self.invoke()
        self.assertIn("Existing update branch was modified", self.record_review.call_args.args[4])

    def test_api_cannot_reconcile_rewritten_history(self):
        os.chdir(self.root / "upstream")
        tree = self.git("rev-parse", self.target + "^{tree}")
        rewritten = self.git("commit-tree", tree, "-m", "rewritten upstream release")
        self.git("tag", "-f", "v1.7.129", rewritten)
        os.chdir(self.root / "fork")
        client = Mock(enabled=True)
        client.state.data = {"repairs": {}}
        with patch.object(sync.conflict_resolver, "Resolver", return_value=client):
            self.invoke()
            self.assertIn("no common history", self.record_review.call_args.args[4])
        client.resolve.assert_not_called()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertIn("review_required=true", (self.root / "outputs").read_text())

    def test_documentation_and_add_add_test_conflicts_are_both_resolved(self):
        os.chdir(self.root / "upstream")
        Path("tests").mkdir()
        Path("tests/FeatureTest.kt").write_text("upstream assertions\n")
        self.commit("new upstream test")
        self.git("tag", "-f", "v1.7.129")
        os.chdir(self.root / "fork")
        Path("tests").mkdir()
        Path("tests/FeatureTest.kt").write_text("fork assertions\n")
        Path("docs/guide.md").write_text("fork prose\n")
        self.commit("fork test and prose")
        self.git("push", "origin", "dev")
        client = Mock(enabled=True)
        client.state.data = {"repairs": {}}
        def repair(conflicts, overview=None):
            for path, _ in conflicts:
                Path(path).write_text("combined prose\n" if path.endswith(".md") else "combined assertions\n")
            return [path for path, _ in conflicts]
        client.repair_update.side_effect = repair
        with patch.object(sync.conflict_resolver, "Resolver", return_value=client):
            self.invoke()
        self.assertEqual(client.repair_update.call_count, 1)
        self.assertEqual(Path("tests/FeatureTest.kt").read_text(), "combined assertions\n")
        client.assess_update.assert_called_once()

    def test_conflict_free_update_skips_codex_and_requires_full_validation(self):
        with patch.object(sync.conflict_resolver, "Resolver") as resolver:
            self.invoke()
        resolver.assert_not_called()
        outputs = (self.root / "outputs").read_text()
        self.assertIn("full_validation=true", outputs)
        self.assertIn(f"head={self.git('rev-parse', 'HEAD')}", outputs)

    def test_review_required_preserves_candidate_patch_and_returns_success(self):
        Path("docs/guide.md").write_text("fork conflicting change\n")
        self.commit("fork change")
        self.base = self.git("rev-parse", "HEAD")
        self.git("push", "origin", "dev")
        client = Mock(enabled=True)
        client.repair_update.side_effect = sync.conflict_resolver.policy.ReviewRequired("breaking interface")
        with patch.object(sync.conflict_resolver, "Resolver", return_value=client):
            self.invoke()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertEqual(self.git("status", "--porcelain"), "")
        outputs = (self.root / "outputs").read_text()
        self.assertIn("review_required=true", outputs)
        self.assertIn("changed=false", outputs)
        self.assertIn("upstream update", self.record_review.call_args.args[5])
        self.assertEqual(self.git("ls-remote", "--heads", "origin", "refs/heads/upstream/*"), "")

    def test_pending_review_skips_codex_and_does_not_create_another_issue(self):
        self.pending_review.return_value = {"html_url": "https://github.com/ajctrl/Sumire/issues/43",
                                            "body": "human review is pending"}
        with patch.object(sync.conflict_resolver, "Resolver") as client:
            self.invoke()
            client.assert_not_called()
        self.record_review.assert_not_called()
        self.assertIn("review_required=true", (self.root / "outputs").read_text())
        self.assertTrue((self.root / "runner-temp/upstream-review/report.md").exists())



if __name__ == "__main__":
    unittest.main()
