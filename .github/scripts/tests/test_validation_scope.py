import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "validation-scope.py"
spec = importlib.util.spec_from_file_location("validation_scope_tests", SCRIPT)
scope = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scope)


class ScopeTests(unittest.TestCase):
    def test_full_native_build_and_dictionary_changes_require_full(self):
        for path in ("app/build.gradle", "settings.gradle", "gradle/libs.versions.toml",
                     "app/src/main/cpp/runtime.cpp", "app/src/full/java/Runtime.kt",
                     "app/src/main/assets/ngram/system.dat", "tools/system_ngram/generate.py",
                     "app/src/main/java/converter/PackedDictionary.kt",
                     "app/src/main/java/zenz/Runtime.kt", ".gitmodules"):
            with self.subTest(path=path):
                self.assertTrue(scope.requires_full([path]))
        self.assertFalse(scope.requires_full(["docs/guide.md", ".github/scripts/sync-upstream.py"]))

    def test_release_number_only_changes_do_not_force_full(self):
        diff = '--- a/app/build.gradle\n+++ b/app/build.gradle\n-versionName "1.7.128"\n+versionName "1.7.129"\n-versionCode 821\n+versionCode 822\n'
        with patch.object(scope.subprocess, "check_output", side_effect=['app/build.gradle\0docs/guide.md\0', diff]):
            self.assertFalse(scope.requires_full_revision('base', 'head'))
        with patch.object(scope.subprocess, "check_output", side_effect=['app/build.gradle\0', diff + '+buildFeatures { viewBinding true }\n']):
            self.assertTrue(scope.requires_full_revision('base', 'head'))

    def test_full_assets_still_trigger_when_gradle_change_is_version_only(self):
        with patch.object(scope.subprocess, "check_output", side_effect=[
                'app/build.gradle\0app/src/main/assets/dictionary.dat\0', '-versionCode 1\n+versionCode 2\n']):
            self.assertTrue(scope.requires_full_revision('base', 'head'))


if __name__ == '__main__':
    unittest.main()
