import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "codex-edits.py"
spec = importlib.util.spec_from_file_location("codex_edits_tests", SCRIPT)
edits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(edits)


class EditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "Feature.kt").write_text("old code")
        (self.root / "obsolete.kt").write_text("old helper")
        self.before = edits.inventory(self.root)

    def test_direct_file_changes_additions_deletions_and_modes_round_trip(self):
        (self.root / "Feature.kt").write_text("merged code")
        (self.root / "obsolete.kt").unlink()
        (self.root / "helper.sh").write_text("echo helper")
        (self.root / "helper.sh").chmod(0o755)
        changed = edits.changes(self.root, self.before)
        with tempfile.TemporaryDirectory() as target:
            (Path(target) / "obsolete.kt").write_text("old helper")
            edits.apply(target, changed)
            self.assertEqual((Path(target) / "Feature.kt").read_text(), "merged code")
            self.assertFalse((Path(target) / "obsolete.kt").exists())
            self.assertTrue((Path(target) / "helper.sh").stat().st_mode & 0o111)

    def test_protected_and_symlink_changes_are_rejected(self):
        (self.root / ".github").mkdir()
        (self.root / ".github" / "workflow.yml").write_text("weaken validation")
        with self.assertRaisesRegex(RuntimeError, "protected"):
            edits.changes(self.root, self.before)
        (self.root / ".github" / "workflow.yml").unlink()
        (self.root / "link").symlink_to(self.root / "Feature.kt")
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            edits.changes(self.root, self.before)

    def test_invalid_restored_paths_are_checked_before_any_file_is_written(self):
        for path in ("../escape", ".github/workflow.yml", "/absolute"):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                edits.apply(self.root, {"Feature.kt": {"text": "changed", "executable": False},
                                        path: {"text": "bad", "executable": False}})
            self.assertEqual((self.root / "Feature.kt").read_text(), "old code")

    def test_binary_changes_are_not_transferred(self):
        (self.root / "Feature.kt").write_bytes(b"binary\0data")
        with self.assertRaisesRegex(RuntimeError, "binary"):
            edits.changes(self.root, self.before)

    def test_deleting_a_binary_is_also_rejected(self):
        binary = self.root / "dictionary.bin"
        binary.write_bytes(b"binary\0data")
        before = edits.inventory(self.root)
        binary.unlink()
        with self.assertRaisesRegex(RuntimeError, "binary"):
            edits.changes(self.root, before)


if __name__ == "__main__":
    unittest.main()
