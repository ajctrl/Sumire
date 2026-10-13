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


if __name__ == '__main__':
    unittest.main()
