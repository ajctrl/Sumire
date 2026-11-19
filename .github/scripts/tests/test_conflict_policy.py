import importlib.util
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "conflict-policy.py"
spec = importlib.util.spec_from_file_location("test_conflict_policy_module", SCRIPT)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def conflict(ours="fork\n", base="old\n", theirs="upstream\n"):
    return f"<<<<<<< ours\n{ours}||||||| base\n{base}=======\n{theirs}>>>>>>> theirs\n"


class PolicyTests(unittest.TestCase):
    def test_application_tests_build_and_sensitive_topics_are_not_blanket_blocked(self):
        for path in ("app/build.gradle", "gradle/libs.versions.toml", "app/src/main/AndroidManifest.xml",
                     "app/src/main/java/Feature.kt", "app/src/test/java/FeatureTest.kt",
                     "app/src/main/java/syncbridge/Clipboard.kt", "docs/SECURITY.md",
                     ".gitattributes", "app/src/main/res/values/strings.xml"):
            with self.subTest(path=path):
                self.assertEqual(policy.check_conflict(path, conflict()), "text")
                policy.validate_replacements(path, conflict(), ["valid merged content\n"])

    def test_large_code_hunks_and_many_hunks_are_not_arbitrarily_blocked(self):
        code = '    val value = "clipboard permission"\n' * 50
        policy.check_conflict("app/src/main/java/Feature.kt", conflict(code) * 5)
        policy.validate_replacements("app/src/main/java/Feature.kt", conflict(), [code])

    def test_automation_noncanonical_binary_and_malformed_conflicts_require_review(self):
        for path in (".github/workflows/android.yml", ".git/config", "../Feature.kt", "/Feature.kt",
                     "app//Feature.kt", "app/./Feature.kt", "app\\Feature.kt"):
            with self.subTest(path=path), self.assertRaises(policy.ReviewRequired):
                policy.check_conflict(path, conflict())
        for content in ("<<<<<<< HEAD\nincomplete\n", conflict() + "=======\n", conflict("a\0b\n"),
                        conflict(conflict())):
            with self.subTest(content=content), self.assertRaises(policy.ReviewRequired):
                policy.check_conflict("Feature.kt", content)

    def test_invalid_generated_replacements_still_require_review(self):
        for replacements in ([], ["first", "extra"], ["<<<<<<< HEAD\n"], ["a\0b"], [None]):
            with self.subTest(replacements=replacements), self.assertRaises(policy.ReviewRequired):
                policy.validate_replacements("Feature.kt", conflict(), replacements)


if __name__ == "__main__":
    unittest.main()
