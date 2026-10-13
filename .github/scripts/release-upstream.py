"""Plan or publish an immutable upstream release; never replace a published asset."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

APK_NAMES = (
    'app-fullStandard-arm64-v8a-release.apk',
    'app-fullStandard-x86_64-release.apk',
    'app-liteStandard-arm64-v8a-release.apk',
    'app-liteStandard-x86_64-release.apk',
    'app-liteFdroid-release.apk',
)
ASSET_NAMES = (*APK_NAMES, 'SHA256SUMS')


def run(*args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr}')
    return result.stdout.strip()


def api(path, method='GET', data=None, missing_ok=False):
    args = ['gh', 'api', path, '--method', method]
    if data is not None:
        args += ['--input', '-']
    result = subprocess.run(args, text=True, capture_output=True,
                            input=json.dumps(data) if data is not None else None)
    if result.returncode:
        if missing_ok and '(HTTP 404)' in result.stderr:
            return None
        raise RuntimeError(f'GitHub API {method} {path}: {result.stderr}')
    return json.loads(result.stdout) if result.stdout else None


def git(*args):
    return run('git', *args)


def version_info(gradle):
    name = re.search(r'^\s*versionName "([0-9]+\.[0-9]+\.[0-9]+)"\s*$', gradle, re.M)
    code = re.search(r'^\s*versionCode ([0-9]+)\s*$', gradle, re.M)
    if not name or not code:
        raise ValueError('Cannot identify the release version in app/build.gradle')
    return name[1], int(code[1])


def complete(release):
    assets = {a['name'] for a in release['assets']
              if a['state'] == 'uploaded' and a['size'] > 0}
    return set(ASSET_NAMES) <= assets


def output(**values):
    with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
        for key, value in values.items():
            stream.write(f'{key}={value}\n')


def assert_source(sha, tag):
    state = json.loads(git('show', f'{sha}:.github/upstream.json'))
    name, _ = version_info(git('show', f'{sha}:app/build.gradle'))
    if state['repository'] != 'KazumaProject/JapaneseKeyboard' or state['tag'] != tag or tag != f'v{name}':
        raise ValueError('Upstream marker and application version do not match')
    git('merge-base', '--is-ancestor', state['commit'], sha)
    git('merge-base', '--is-ancestor', sha, 'origin/dev')


def plan(repo):
    sha = git('rev-parse', 'HEAD')
    state = json.loads(Path('.github/upstream.json').read_text())
    tag = state['tag']
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag):
        raise ValueError('Expected a stable upstream tag')
    assert_source(sha, tag)
    if os.environ.get('GITHUB_REF_TYPE') == 'tag' and os.environ['GITHUB_REF_NAME'] != tag:
        raise ValueError('Pushed tag differs from the application version')
    release = api(f'repos/{repo}/releases/tags/{tag}', missing_ok=True)
    if release and not release['draft']:
        if release['prerelease'] or not complete(release):
            raise ValueError('Existing public release is incomplete; manual review required')
        output(needed='false', tag=tag)
        print(f'{tag} is already published; leaving its tag and assets unchanged')
        return
    # If a previous attempt created the tag, retry exactly that commit, even if dev moved.
    existing = git('tag', '--list', tag)
    if existing:
        sha = git('rev-parse', f'refs/tags/{tag}^{{commit}}')
        assert_source(sha, tag)
    output(needed='true', tag=tag, sha=sha)
    print(f'Will validate, sign and publish {tag} from {sha}')


def verify_manifest(directory, tag, sha):
    manifest = json.loads((directory / 'provenance.json').read_text())
    if manifest['tag'] != tag or manifest['sha'] != sha or set(manifest['files']) != set(ASSET_NAMES):
        raise ValueError('Release artifact does not match the planned source and file set')
    for name, digest in manifest['files'].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'Artifact checksum mismatch: {name}')


def publish(repo, tag, sha, directory):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag) or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('Invalid release identity')
    verify_manifest(directory, tag, sha)
    release = api(f'repos/{repo}/releases/tags/{tag}', missing_ok=True)
    if release and not release['draft']:
        if complete(release) and not release['prerelease']:
            print('Already published; nothing to overwrite')
            return
        raise ValueError('Refusing to overwrite a public release')
    reference = api(f'repos/{repo}/git/ref/tags/{tag}', missing_ok=True)
    if reference is None:
        api(f'repos/{repo}/git/refs', 'POST', {'ref': f'refs/tags/{tag}', 'sha': sha})
    else:
        target = api(f'repos/{repo}/commits/{tag}')['sha']
        if target != sha:
            raise ValueError('Refusing to move an existing release tag')
    if release is None:
        release = api(f'repos/{repo}/releases', 'POST', {
            'tag_name': tag, 'target_commitish': sha, 'name': tag,
            'body': f'Upstream {tag} with the Sumire Sync clipboard bridge.\n\nSource: {sha}\n',
            'draft': True, 'prerelease': False, 'generate_release_notes': True,
        })
    run('gh', 'release', 'upload', tag, '--repo', repo, '--clobber',
        *(str(directory / name) for name in ASSET_NAMES))
    uploaded = api(f"repos/{repo}/releases/{release['id']}")
    if not uploaded['draft'] or not complete(uploaded):
        raise ValueError('Draft release is incomplete or was changed externally')
    # Compare GitHub's uploaded asset digests with the signed build artifact.
    expected = json.loads((directory / 'provenance.json').read_text())['files']
    for asset in uploaded['assets']:
        if asset['name'] in expected and asset.get('digest') != 'sha256:' + expected[asset['name']]:
            raise ValueError(f"Uploaded digest mismatch: {asset['name']}")
    api(f"repos/{repo}/releases/{release['id']}", 'PATCH', {'draft': False, 'make_latest': 'true'})
    print(f'https://github.com/{repo}/releases/tag/{tag}')


if __name__ == '__main__':
    repo = os.environ['GITHUB_REPOSITORY']
    if repo != 'ajctrl/Sumire':
        raise SystemExit('Release automation is restricted to ajctrl/Sumire')
    if sys.argv[1:] == ['plan']:
        plan(repo)
    elif sys.argv[1:] == ['publish']:
        publish(repo, os.environ['RELEASE_TAG'], os.environ['RELEASE_SHA'],
                Path(os.environ['RELEASE_DIRECTORY']))
    else:
        raise SystemExit('Usage: release-upstream.py plan|publish')
