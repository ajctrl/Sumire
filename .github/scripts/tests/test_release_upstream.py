import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


release = module('release', 'release-upstream.py')
verify = module('verify', 'verify-release-apks.py')
SHA = 'a' * 40
TAG = 'v1.7.128'
REPO = 'ajctrl/Sumire'


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        old_cwd = Path.cwd()
        self.addCleanup(os.chdir, old_cwd)
        os.chdir(self.root)
        Path('app/build/outputs/apk').mkdir(parents=True)
        Path('app/build.gradle').write_text('versionName "1.7.128"\nversionCode 821\n')
        for name in release.APK_NAMES:
            (Path('app/build/outputs/apk') / name).write_bytes(name.encode())
        self.target = self.root / 'release'
        env = patch.dict(os.environ, ANDROID_HOME='/sdk', PREVIOUS_APK='/previous.apk',
                         RELEASE_DIRECTORY=str(self.target), RELEASE_TAG=TAG, RELEASE_SHA=SHA,
                         GITHUB_OUTPUT=str(self.root / 'outputs'), GITHUB_REF_TYPE='branch')
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def identity(tool, apk):
        name = apk.name
        if name == 'previous.apk':
            return 'com.example.sumire', 805, '1.7.111'
        lite = 'liteStandard' in name
        fdroid = 'liteFdroid' in name
        suffix = '.lite.fdroid' if fdroid else '.lite' if lite else ''
        version_suffix = '-lite-fdroid' if fdroid else '-lite' if lite else ''
        offset = 1 if 'arm64-v8a' in name else 2 if 'x86_64' in name else 0
        return 'com.example.sumire' + suffix, 821 + offset, '1.7.128' + version_suffix

    def make_artifacts(self):
        with patch.object(verify, 'certificate', return_value='abc'), \
                patch.object(verify, 'identity', side_effect=self.identity):
            verify.main()

    def uploaded(self, draft=True):
        files = json.loads((self.target / 'provenance.json').read_text())['files']
        return {'id': 123, 'draft': draft, 'prerelease': False,
                'assets': [{'name': name, 'state': 'uploaded', 'size': 10,
                            'digest': 'sha256:' + digest} for name, digest in files.items()]}

    def test_all_five_packages_and_manifest(self):
        self.make_artifacts()
        release.verify_manifest(self.target, TAG, SHA)
        self.assertEqual(len(list(self.target.glob('*.apk'))), 5)

    def test_manual_revision_verifies_and_publishes_all_five_packages(self):
        tag = 'v1.7.128.2'
        Path('app/build.gradle').write_text('versionName "1.7.128.2"\nversionCode 823\n')

        def identity(tool, apk):
            package, code, name = self.identity(tool, apk)
            if apk.name != 'previous.apk':
                code += 2
                name = name.replace('1.7.128', '1.7.128.2')
            return package, code, name

        with patch.dict(os.environ, RELEASE_TAG=tag), \
                patch.object(verify, 'certificate', return_value='abc'), \
                patch.object(verify, 'identity', side_effect=identity):
            verify.main()
        release.verify_manifest(self.target, tag, SHA)
        replies = [None, None, {}, {'id': 123}, self.uploaded(), {}]
        with patch.object(release, 'api', side_effect=replies) as api, \
                patch.object(release, 'run'):
            release.publish(REPO, tag, SHA, self.target)
        created = api.call_args_list[3].args[2]
        self.assertEqual(created['tag_name'], tag)
        self.assertIn('Upstream: v1.7.128\n', created['body'])

    def test_reject_signer_change(self):
        with patch.object(verify, 'certificate', side_effect=['old', 'new']), \
                patch.object(verify, 'identity', side_effect=self.identity):
            with self.assertRaisesRegex(ValueError, 'Signing certificate differs'):
                verify.main()

    def test_reject_non_increasing_version_code(self):
        with patch.object(verify, 'certificate', return_value='abc'), \
                patch.object(verify, 'identity', return_value=('com.example.sumire', 822, '1.7.128')):
            with self.assertRaisesRegex(ValueError, 'versionCode must increase'):
                verify.main()

    def test_reject_missing_apk(self):
        (Path('app/build/outputs/apk') / release.APK_NAMES[-1]).unlink()
        with self.assertRaisesRegex(ValueError, 'exactly one built APK'):
            self.make_artifacts()

    def test_reject_altered_artifact(self):
        self.make_artifacts()
        (self.target / release.APK_NAMES[0]).write_bytes(b'altered')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            release.verify_manifest(self.target, TAG, SHA)

    def test_publish_only_after_all_assets_verified(self):
        self.make_artifacts()
        replies = [None, None, {}, {'id': 123}, self.uploaded(), {}]
        with patch.object(release, 'api', side_effect=replies) as api, \
                patch.object(release, 'run') as run:
            release.publish(REPO, TAG, SHA, self.target)
        self.assertEqual(api.call_args_list[2].args[2], {'ref': f'refs/tags/{TAG}', 'sha': SHA})
        self.assertTrue(api.call_args_list[3].args[2]['draft'])
        self.assertEqual(api.call_args_list[-1].args[2], {'draft': False, 'make_latest': 'true'})
        self.assertIn('--clobber', run.call_args.args)

    def test_public_release_is_never_overwritten(self):
        self.make_artifacts()
        with patch.object(release, 'api', return_value=self.uploaded(draft=False)) as api, \
                patch.object(release, 'run') as run:
            release.publish(REPO, TAG, SHA, self.target)
        self.assertEqual(api.call_count, 1)
        run.assert_not_called()

    def test_existing_tag_is_never_moved(self):
        self.make_artifacts()
        with patch.object(release, 'api', side_effect=[None, {'ref': TAG}, {'sha': 'b' * 40}]), \
                patch.object(release, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Refusing to move'):
                release.publish(REPO, TAG, SHA, self.target)
        run.assert_not_called()

    def test_bad_uploaded_digest_keeps_release_draft(self):
        self.make_artifacts()
        uploaded = self.uploaded()
        uploaded['assets'][0]['digest'] = 'sha256:bad'
        with patch.object(release, 'api', side_effect=[None, None, {}, {'id': 123}, uploaded]) as api, \
                patch.object(release, 'run'):
            with self.assertRaisesRegex(ValueError, 'Uploaded digest mismatch'):
                release.publish(REPO, TAG, SHA, self.target)
        self.assertFalse(any(call.args[1:2] == ('PATCH',) for call in api.call_args_list))

    def test_retry_uses_existing_tag_commit(self):
        Path('.github').mkdir()
        Path('.github/upstream.json').write_text(json.dumps({'tag': TAG}))
        with patch.object(release, 'git', side_effect=[SHA, TAG, 'b' * 40]), \
                patch.object(release, 'assert_source') as source, \
                patch.object(release, 'api', return_value=None):
            release.plan(REPO)
        self.assertEqual(source.call_args.args, ('b' * 40, TAG))
        self.assertIn('sha=' + 'b' * 40, Path('outputs').read_text())

    def test_api_forbidden_is_not_treated_as_missing(self):
        result = subprocess.CompletedProcess([], 1, '', 'gh: Forbidden (HTTP 403)')
        with patch.object(release.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 403'):
                release.api('repos/example/release', missing_ok=True)


class ReleasePlanningTests(unittest.TestCase):
    """Exercise source/ancestry and tag checks against real, disposable Git history."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        old_cwd = Path.cwd()
        self.addCleanup(os.chdir, old_cwd)
        os.chdir(self.root)
        self.git('init', '-b', 'dev')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        Path('app').mkdir()
        Path('app/build.gradle').write_text('versionName "1.7.128"\nversionCode 821\n')
        self.commit('upstream')
        upstream = self.git('rev-parse', 'HEAD')
        Path('.github').mkdir()
        Path('.github/upstream.json').write_text(json.dumps({
            'repository': 'KazumaProject/JapaneseKeyboard', 'tag': TAG, 'commit': upstream}))
        self.commit('fork')
        self.env = patch.dict(os.environ, GITHUB_OUTPUT=str(self.root / 'outputs'),
                              GITHUB_REF_TYPE='branch', UPSTREAM_ONLY='false')
        self.env.start()
        self.addCleanup(self.env.stop)

    def git(self, *args):
        return subprocess.run(['git', *args], check=True, text=True,
                              capture_output=True).stdout.strip()

    def commit(self, message):
        self.git('add', 'app', *(['.github'] if Path('.github').exists() else []))
        self.git('commit', '-m', message)
        self.git('update-ref', 'refs/remotes/origin/dev', 'HEAD')

    def revision(self, name='1.7.128.2'):
        Path('app/build.gradle').write_text(f'versionName "{name}"\nversionCode 823\n')
        self.commit('manual revision')

    def test_automatic_upstream_release_still_plans(self):
        with patch.dict(os.environ, UPSTREAM_ONLY='true'), \
                patch.object(release, 'api', return_value=None):
            release.plan(REPO)
        self.assertIn(f'needed=true\ntag={TAG}\n', Path('outputs').read_text())

    def test_manual_tag_and_dispatch_use_application_version_without_changing_marker(self):
        self.revision()
        marker = Path('.github/upstream.json').read_text()
        for kind in ('tag', 'branch'):
            with self.subTest(kind=kind), \
                    patch.dict(os.environ, GITHUB_REF_TYPE=kind, GITHUB_REF_NAME='v1.7.128.2'), \
                    patch.object(release, 'api', return_value=None) as api:
                release.plan(REPO)
                api.assert_called_once_with(f'repos/{REPO}/releases/tags/v1.7.128.2', missing_ok=True)
        self.assertIn('needed=true\ntag=v1.7.128.2\n', Path('outputs').read_text())
        self.assertEqual(Path('.github/upstream.json').read_text(), marker)

    def test_automatic_sync_never_publishes_manual_revision(self):
        self.revision()
        with patch.dict(os.environ, UPSTREAM_ONLY='true'), patch.object(release, 'api') as api:
            release.plan(REPO)
        api.assert_not_called()
        self.assertIn('needed=false', Path('outputs').read_text())

    def test_mismatched_pushed_tag_is_rejected(self):
        self.revision()
        with patch.dict(os.environ, GITHUB_REF_TYPE='tag', GITHUB_REF_NAME='v1.7.128.3'), \
                patch.object(release, 'api') as api:
            with self.assertRaisesRegex(ValueError, 'Pushed tag differs'):
                release.plan(REPO)
        api.assert_not_called()

    def test_different_upstream_base_is_rejected(self):
        self.revision('1.7.129.2')
        with self.assertRaisesRegex(ValueError, 'Upstream marker'):
            release.assert_source(self.git('rev-parse', 'HEAD'), 'v1.7.129.2')

    def test_unmerged_source_is_rejected(self):
        base = self.git('rev-parse', 'HEAD')
        self.revision()
        self.git('update-ref', 'refs/remotes/origin/dev', base)
        with self.assertRaisesRegex(RuntimeError, 'git failed'):
            release.assert_source(self.git('rev-parse', 'HEAD'), 'v1.7.128.2')

    def test_manual_retry_keeps_original_tag_commit(self):
        self.revision()
        tagged = self.git('rev-parse', 'HEAD')
        self.git('tag', 'v1.7.128.2')
        Path('app/new.txt').write_text('later change')
        self.commit('later fix')
        with patch.object(release, 'api', return_value=None):
            release.plan(REPO)
        self.assertIn(f'sha={tagged}\n', Path('outputs').read_text())

    def test_reject_invalid_revision_formats(self):
        for name in ('1.7.128.0', '1.7.128.01', '1.7.128.2.1', '1.7.128-rc1'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.version_info(f'versionName "{name}"\nversionCode 823\n')


if __name__ == '__main__':
    unittest.main()
