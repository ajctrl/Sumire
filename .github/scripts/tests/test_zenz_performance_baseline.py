import importlib.util
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    "zenz_baseline", Path(__file__).resolve().parents[1] / "zenz-performance-baseline.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ZenzPerformanceBaselineTest(unittest.TestCase):
    def metadata(self, commit, repository="KazumaProject/JapaneseKeyboard"):
        return json.dumps({"commit": commit, "repository": repository})

    def test_normal_change_keeps_pr_base(self):
        with patch.object(module, "git", side_effect=["a" * 40, self.metadata("b" * 40), self.metadata("b" * 40)]):
            self.assertEqual("a" * 40, module.select_baseline("dev"))

    def test_release_sync_uses_imported_release(self):
        with patch.object(module, "git", side_effect=["a" * 40, self.metadata("b" * 40), self.metadata("c" * 40)]), patch.object(module.subprocess, "run") as run:
            self.assertEqual("c" * 40, module.select_baseline("dev"))
            run.assert_called_once_with(["git", "merge-base", "--is-ancestor", "c" * 40, "HEAD"], check=True)

    def test_unmerged_release_is_rejected(self):
        with patch.object(module, "git", side_effect=["a" * 40, self.metadata("b" * 40), self.metadata("c" * 40)]), patch.object(module.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "git")):
            with self.assertRaises(subprocess.CalledProcessError):
                module.select_baseline("dev")

    def test_invalid_metadata_is_rejected(self):
        for metadata in [self.metadata("c" * 40, "other/repo"), self.metadata("invalid\nOTHER_ENV=value")]:
            with self.subTest(metadata=metadata), patch.object(module, "git", side_effect=["a" * 40, self.metadata("b" * 40), metadata]):
                with self.assertRaises(ValueError):
                    module.select_baseline("dev")
