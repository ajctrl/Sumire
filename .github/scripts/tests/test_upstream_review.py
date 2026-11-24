import importlib.util
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "upstream-review.py"
spec = importlib.util.spec_from_file_location("upstream_review_tests", SCRIPT)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


class ReviewTests(unittest.TestCase):
    def test_review_is_created_once_and_closed_draft_does_not_authorize_merging(self):
        pulls = []
        writes = []
        def api(path, payload=None):
            if payload:
                writes.append(payload)
                pulls.append(dict(payload, user={'login': 'github-actions[bot]'}, state='open',
                                   html_url='https://github.com/ajctrl/Sumire/pull/43'))
                return pulls[-1]
            return pulls
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, RUNNER_TEMP=directory), \
                patch.object(review, 'api', side_effect=api), \
                patch.object(review, 'report_branch', return_value='upstream-review/test'):
            first = review.record('ajctrl/Sumire', 'a'*40, 'b'*40, 'v1.7.150', 'history diverged', 'candidate diff')
            pulls[0]['state'] = 'closed'
            second = review.record('ajctrl/Sumire', 'a'*40, 'b'*40, 'v1.7.150', 'same update')
            self.assertEqual(first['html_url'], second['html_url'])
            self.assertEqual(len(writes), 1)
            self.assertTrue(writes[0]["draft"])
            evidence = Path(directory) / 'upstream-review'
            self.assertIn('same update', (evidence/'report.md').read_text())
            self.assertEqual((evidence/'candidate.patch').read_text(), 'candidate diff')

    def test_report_branch_is_review_only_and_preserves_worktree_and_index(self):
        old = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source'
            remote = root / 'remote.git'
            source.mkdir()
            def git(*args):
                return subprocess.check_output(['git', *args], text=True, stderr=subprocess.DEVNULL).strip()
            try:
                os.chdir(source)
                git('init', '-b', 'dev')
                git('config', 'user.name', 'Test')
                git('config', 'user.email', 'test@example.invalid')
                Path('application.kt').write_text('original source')
                git('add', '.')
                git('commit', '-m', 'base')
                base = git('rev-parse', 'HEAD')
                git('init', '--bare', str(remote))
                git('remote', 'add', 'origin', str(remote))
                git('push', 'origin', 'dev')
                index_tree = git('write-tree')
                branch = review.report_branch(base, 'b'*40, 'v1.7.150', 'reason and artifact link')
                commit = git('--git-dir', str(remote), 'rev-parse', 'refs/heads/' + branch)
                self.assertEqual(git('--git-dir', str(remote), 'show', '-s', '--format=%P', commit), base)
                paths = git('--git-dir', str(remote), 'diff', '--name-only', base, commit).splitlines()
                self.assertEqual(paths, [f'docs/upstream-reviews/{base[:12]}-bbbbbbbbbbbb.md'])
                self.assertEqual(git('write-tree'), index_tree)
                self.assertEqual(git('status', '--porcelain'), '')
                self.assertEqual(review.report_branch(base, 'b'*40, 'v1.7.150', 'new attempt'), branch)
                self.assertEqual(git('--git-dir', str(remote), 'rev-parse', 'refs/heads/' + branch), commit)
            finally:
                os.chdir(old)

    def test_other_users_cannot_spoof_the_pending_review_marker(self):
        fake = {'body': review.marker('base', 'up'), 'user': {'login': 'untrusted-user'}}
        with patch.object(review, 'api', return_value=[fake]):
            self.assertIsNone(review.find('ajctrl/Sumire', 'base', 'up'))
        self.assertNotEqual(review.marker('base', 'up'), review.marker('new-base', 'up'))


if __name__ == '__main__':
    unittest.main()
