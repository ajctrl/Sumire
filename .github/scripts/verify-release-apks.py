"""Verify package identity, versions and signing continuity before publishing APKs."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

spec = importlib.util.spec_from_file_location('release', Path(__file__).with_name('release-upstream.py'))
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def certificate(tool, apk):
    result = subprocess.run([str(tool), 'verify', '--print-certs', str(apk)],
                            check=True, text=True, capture_output=True)
    digests = re.findall(r'Signer #[0-9]+ certificate SHA-256 digest: ([0-9a-f]+)', result.stdout)
    if len(digests) != 1:
        raise ValueError(f'Expected one verified signing certificate: {apk.name}')
    return digests[0]


def identity(tool, apk):
    text = subprocess.run([str(tool), 'dump', 'badging', str(apk)],
                          check=True, text=True, capture_output=True).stdout
    match = re.search(r"package: name='([^']+)' versionCode='([0-9]+)' versionName='([^']+)'", text)
    if not match:
        raise ValueError(f'Cannot read APK identity: {apk.name}')
    return match[1], int(match[2]), match[3]


def main():
    sdk = Path(os.environ['ANDROID_HOME']) / 'build-tools/35.0.0'
    previous = Path(os.environ['PREVIOUS_APK'])
    signer = certificate(sdk / 'apksigner', previous)
    old_package, old_code, _ = identity(sdk / 'aapt', previous)
    if old_package != 'com.example.sumire':
        raise ValueError('Previous release has an unexpected application ID')
    version, code = release.version_info(Path('app/build.gradle').read_text())
    if os.environ['RELEASE_TAG'] != 'v' + version:
        raise ValueError('APK version does not match release tag')
    if code + 1 <= old_code:
        raise ValueError('Release versionCode must increase for in-place upgrades')
    target = Path(os.environ['RELEASE_DIRECTORY'])
    target.mkdir(parents=True, exist_ok=True)
    for name in release.APK_NAMES:
        matches = list(Path('app/build/outputs/apk').rglob(name))
        if len(matches) != 1:
            raise ValueError(f'Expected exactly one built APK: {name}')
        apk = matches[0]
        suffix = '.lite.fdroid' if 'liteFdroid' in name else '.lite' if 'liteStandard' in name else ''
        version_suffix = '-lite-fdroid' if 'liteFdroid' in name else '-lite' if 'liteStandard' in name else ''
        offset = 1 if 'arm64-v8a' in name else 2 if 'x86_64' in name else 0
        if identity(sdk / 'aapt', apk) != ('com.example.sumire' + suffix, code + offset, version + version_suffix):
            raise ValueError(f'Unexpected package or version: {name}')
        if certificate(sdk / 'apksigner', apk) != signer:
            raise ValueError(f'Signing certificate differs from the previous release: {name}')
        shutil.copy2(apk, target / name)
    files = {name: hashlib.sha256((target / name).read_bytes()).hexdigest() for name in release.APK_NAMES}
    sums = target / 'SHA256SUMS'
    sums.write_text(''.join(f'{digest}  {name}\n' for name, digest in files.items()))
    files['SHA256SUMS'] = hashlib.sha256(sums.read_bytes()).hexdigest()
    (target / 'provenance.json').write_text(json.dumps({
        'tag': os.environ['RELEASE_TAG'], 'sha': os.environ['RELEASE_SHA'],
        'signer_sha256': signer, 'files': files,
    }, indent=2) + '\n')
    print(f'All five APKs verified; signing certificate SHA-256: {signer}')


if __name__ == '__main__':
    main()
