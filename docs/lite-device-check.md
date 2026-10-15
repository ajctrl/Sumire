# Floating English QWERTY: Lite device check

This local branch starts at fork `dev` commit `7318d297` and includes the
floating English QWERTY fix. It retains the Sumire package ID and SUMIRE-SYNC
integration. The upstream PR branch remains separate.

## Starting the build

After approval, push `test/floating-english-qwerty-release` to `origin`.
That push starts **Signed Lite device check**. Subsequent pushes to this branch
also start a build. No default-branch change is needed for this push trigger.
The workflow has read-only repository permissions and does not create a PR,
tag or GitHub Release. The existing release workflow is unchanged.

The workflow runs the floating input mode, English QWERTY resolver and split
keyboard tests, then builds the Lite Standard release with existing signing
Secrets. It compares each ABI's APK with the latest published Lite APK:

- Package must be `com.example.sumire.lite`.
- Signing certificate must match.
- versionCode must be equal or greater. Same-version replacement is intentional;
  no extra version bump is needed just for this device check.

On success, download the `lite-device-check-<commit>-<attempt>` artifact from
the Actions run. It contains arm64-v8a and x86_64 APKs, SHA256SUMS and provenance
with the exact source commit and baseline release tag. Artifacts expire after
14 days; keep the APK locally if needed. Secrets and keystore are not uploaded.

## Installing and checking

For a typical ARM64 Android phone, extract and open
`app-liteStandard-arm64-v8a-release.apk` on the phone and choose Update.
Use the matching ABI on other devices. Do not uninstall the existing app.
With an authorized ADB connection, the equivalent is:

```sh
adb install -r app-liteStandard-arm64-v8a-release.apk
```

Compatibility is checked against the published APK, not the app actually on
the phone. If Android refuses the update, inspect the installed package,
signature and version before proceeding; do not uninstall or force a downgrade.

Check these behaviors on the installed release build:

1. Enable the existing English QWERTY preference. In floating mode, switch
   Japanese flick to English: English QWERTY appears and types Latin letters.
2. Return to Japanese flick, then switch to English again.
3. Disable the preference: English remains flick. Restore your preferred setting.
4. Move and resize the floating keyboard and return to normal docked mode.
5. Confirm ordinary conversion and clipboard synchronization with SUMIRE-SYNC.

The signed release APK can remain installed for daily use after checking.
The source fix must later be merged into fork dev (or brought in through
upstream synchronization) so subsequent releases retain the behavior.

## Local preparation checks

The fork integration check, Python test suite (31 tests), and actionlint
validation of the new workflow pass locally.
The upstream-based fix previously passed its Android build and 11 related tests.
Android tests and the signed release build on this fork-based branch are pending
the Actions run; no release APK has yet been built from this branch.
