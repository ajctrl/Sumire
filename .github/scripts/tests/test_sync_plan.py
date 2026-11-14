import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "sync-plan.py"
spec = importlib.util.spec_from_file_location("sync_plan_tests", SCRIPT)
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)


class PlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.file = self.root / "sync-plan.json"
        self.output = self.root / "output"
        env = patch.dict(os.environ, GITHUB_REPOSITORY="ajctrl/Sumire", GITHUB_RUN_ID="42",
                         GITHUB_RUN_ATTEMPT="2", EXPECTED_RUN_ID="42", EXPECTED_RUN_ATTEMPT="2",
                         GITHUB_OUTPUT=str(self.output), SYNC_CHANGED="true", SYNC_PAUSED="false",
                         SYNC_REVIEW_REQUIRED="false", SYNC_FULL_VALIDATION="false",
                         SYNC_BASE="a" * 40, SYNC_HEAD="b" * 40, SYNC_PR="123")
        env.start()
        self.addCleanup(env.stop)

    def test_changed_plan_passes_exact_head_and_base(self):
        plan.write(self.file)
        plan.read(self.file)
        self.assertEqual(self.output.read_text(),
                         f"changed=true\npaused=false\nbase={'a' * 40}\nhead={'b' * 40}\npr=123\nreview_required=false\nfull_validation=false\n")

    def test_paused_and_unchanged_plans_do_not_carry_updates(self):
        for paused in ("true", "false"):
            with self.subTest(paused=paused), patch.dict(os.environ, SYNC_CHANGED="false",
                                                       SYNC_PAUSED=paused, SYNC_HEAD="", SYNC_PR=""):
                plan.write(self.file)
                plan.read(self.file)
                self.assertIn(f"changed=false\npaused={paused}\n", self.output.read_text())
                self.output.unlink()

    def test_other_repository_run_or_attempt_is_rejected_before_outputs(self):
        for field, value in (("repository", "fork/Sumire"), ("run_id", 41), ("run_attempt", 1)):
            plan.write(self.file)
            data = json.loads(self.file.read_text())
            data[field] = value
            self.file.write_text(json.dumps(data))
            with self.subTest(field=field), self.assertRaises(ValueError):
                plan.read(self.file)
            self.assertFalse(self.output.exists())

    def test_invalid_plan_or_output_injection_is_rejected(self):
        for changes in ({"changed": True, "paused": True}, {"head": "b" * 40 + "\npaused=false"},
                        {"base": ""}, {"pr": "123\npaused=false"}, {"pr": True},
                        {"changed": False}, {"changed": "false"}, {"unexpected": "value"}):
            plan.write(self.file)
            data = json.loads(self.file.read_text())
            data.update(changes)
            self.file.write_text(json.dumps(data))
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                plan.read(self.file)
            self.assertFalse(self.output.exists())

    def test_review_waiting_plan_cannot_trigger_merge_or_release(self):
        with patch.dict(os.environ, SYNC_CHANGED="false", SYNC_PAUSED="false", SYNC_HEAD="", SYNC_PR="",
                        SYNC_REVIEW_REQUIRED="true"):
            plan.write(self.file)
            plan.read(self.file)
            self.assertIn("review_required=true", self.output.read_text())
        for field in ("changed", "paused", "full_validation"):
            data = json.loads(self.file.read_text())
            data[field] = True
            self.file.write_text(json.dumps(data))
            with self.subTest(field=field), self.assertRaises(ValueError):
                plan.read(self.file)
            data[field] = False
            self.file.write_text(json.dumps(data))

    def test_missing_preparation_outcome_is_not_assumed_successful(self):
        with patch.dict(os.environ, SYNC_CHANGED=""), self.assertRaises(ValueError):
            plan.write(self.file)
        self.assertFalse(self.file.exists())


if __name__ == "__main__":
    unittest.main()
