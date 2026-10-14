import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


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
        Path("feature.txt").write_text("base\n")
        Path("app").mkdir()
        Path("app/build.gradle").write_text(
            'android {\n    versionName "1.7.128"\n    versionCode 821\n}\n')
        self.commit("initial")
        self.initial = self.git("rev-parse", "HEAD")
        self.git("clone", "--bare", ".", str(self.root / "origin.git"))
        self.git("clone", str(self.root / "origin.git"), str(self.root / "fork"))
        Path("feature.txt").write_text("upstream update\n")
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
                              GITHUB_OUTPUT=str(self.root / "outputs"))
        self.env.start()
        self.addCleanup(self.env.stop)
        self.real_run = sync.run

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

    def test_merge_preserves_fork_and_ancestry_and_retry(self):
        self.invoke()
        first = self.git("rev-parse", "HEAD")
        self.assertEqual(Path("fork.txt").read_text(), "keep me\n")
        self.assertEqual(Path("feature.txt").read_text(), "upstream update\n")
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
        Path("feature.txt").write_text("fork conflicting change\n")
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
        self.invoke()
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
        with self.assertRaisesRegex(RuntimeError, 'non-version conflicts'):
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
        with self.assertRaisesRegex(RuntimeError, "manual review required"):
            self.invoke()
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)


if __name__ == "__main__":
    unittest.main()
