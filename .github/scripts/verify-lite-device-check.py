"""Verify signed Lite APKs for in-place device testing; never publish a release."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil

spec = importlib.util.spec_from_file_location(
    'apk_verifier', Path(__file__).with_name('verify-release-apks.py'))
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def check_upgrade(previous_identity, current_identity, previous_signer, current_signer):
    if previous_identity[0] != 'com.example.sumire.lite' or current_identity[0] != previous_identity[0]:
        raise ValueError('Expected the existing Sumire Lite package ID')
    # Android permits same-version replacement. Do not consume a future release's code.
    if current_identity[1] < previous_identity[1]:
        raise ValueError('APK would downgrade the published Lite versionCode')
    if current_signer != previous_signer:
        raise ValueError('Signing certificate differs from the published Lite APK')


def main():
    sdk = Path(os.environ['ANDROID_HOME']) / 'build-tools/35.0.0'
    previous = Path(os.environ['PREVIOUS_LITE_DIRECTORY'])
    target = Path(os.environ['DEVICE_CHECK_DIRECTORY'])
    version, code = verifier.release.version_info(Path('app/build.gradle').read_text())
    verified = []
    for abi, offset in [('arm64-v8a', 1), ('x86_64', 2)]:
        name = f'app-liteStandard-{abi}-release.apk'
        matches = list(Path('app/build/outputs/apk').rglob(name))
        if len(matches) != 1:
            raise ValueError(f'Expected exactly one APK: {name}')
        apk = matches[0]
        identity = verifier.identity(sdk / 'aapt', apk)
        signer = verifier.certificate(sdk / 'apksigner', apk)
        if identity != ('com.example.sumire.lite', code + offset, version + '-lite'):
            raise ValueError(f'Unexpected built APK version: {name}')
        check_upgrade(verifier.identity(sdk / 'aapt', previous / name), identity,
                      verifier.certificate(sdk / 'apksigner', previous / name), signer)
        verified.append((apk, identity, signer))
    target.mkdir(parents=True, exist_ok=True)
    records = []
    for apk, identity, signer in verified:
        shutil.copy2(apk, target / apk.name)
        records.append({'file': apk.name, 'package': identity[0], 'versionCode': identity[1],
                        'versionName': identity[2], 'signer_sha256': signer,
                        'sha256': hashlib.sha256(apk.read_bytes()).hexdigest()})
    (target / 'SHA256SUMS').write_text(''.join(f"{r['sha256']}  {r['file']}\n" for r in records))
    (target / 'provenance.json').write_text(json.dumps({
        'sha': os.environ['DEVICE_CHECK_SHA'],
        'baseline_tag': Path(os.environ['BASELINE_TAG_FILE']).read_text().strip(),
        'apks': records,
    }, indent=2) + '\n')
    print('Both Lite APKs verified for package, version and signing continuity.')


if __name__ == '__main__':
    main()
