import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "repair-state.py"
spec = importlib.util.spec_from_file_location("repair_state_tests", SCRIPT)
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


def snapshot(revision=0):
    return {"version": 1, "revision": revision, "date": "2026-11-14",
            "used_tokens": 100, "pending": {}, "repairs": {}}


def archive(data):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as zipped:
        zipped.writestr("repair-state.json", json.dumps(data))
    return stream.getvalue()


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, OPENAI_BUDGET_FILE=str(self.root / "repair-state.json"),
                         GITHUB_REPOSITORY="ajctrl/Sumire", GITHUB_REPOSITORY_ID="99",
                         SUMIRE_ARTIFACT_CLIENT="")
        env.start()
        self.addCleanup(env.stop)

    def test_unknown_request_remains_charged_across_same_day_restart(self):
        first = state.RepairState()
        first.reserve(9000, 10000)
        second = state.RepairState()
        with self.assertRaises(state.BudgetPaused):
            second.reserve(2000, 10000)
        self.assertEqual(sum(second.data["pending"].values()), 9000)

    def test_cache_details_are_reported_without_discounting_the_daily_limit(self):
        saved = dict(snapshot(), date=state.today())
        Path(os.environ["OPENAI_BUDGET_FILE"]).write_text(json.dumps(saved))
        current = state.RepairState()
        usage = {"total_tokens": 800, "input_tokens": 700,
                 "input_tokens_details": {"cached_tokens": 600}}
        current.settle(current.reserve(900, 1000), {"usage": usage})
        current.save()
        restored = state.RepairState()
        self.assertEqual(restored.data["cached_input_tokens"], 600)
        self.assertEqual(restored.data["tokens_without_cache_details"], 100)
        self.assertEqual(restored.remaining(1000), 100)
        with self.assertRaises(state.BudgetPaused):
            restored.reserve(101, 1000)
        with patch.object(state, "today", return_value="2100-01-01"):
            self.assertEqual(restored.remaining(1000), 1000)
        self.assertEqual(restored.data["cached_input_tokens"], 0)
        self.assertEqual(restored.data["tokens_without_cache_details"], 0)

    def test_unknown_cache_usage_is_distinct_from_a_reported_zero(self):
        current = state.RepairState()
        for usage in [{}, {"total_tokens": 50}, {"total_tokens": 50, "input_tokens": 30,
                                                "input_tokens_details": {"cached_tokens": 40}}]:
            current.settle(current.reserve(100, 1000), {"usage": usage})
        current.settle(current.reserve(100, 1000), {"usage": {
            "total_tokens": 90, "input_tokens": 80, "input_tokens_details": {"cached_tokens": 0}}})
        self.assertEqual(current.data["used_tokens"], 290)
        self.assertEqual(current.data["cached_input_tokens"], 0)
        self.assertEqual(current.data["tokens_without_cache_details"], 200)

    def test_restore_filters_source_and_uses_latest_revision_for_timestamp_ties(self):
        def artifact(identifier, run, slot, **changes):
            result = {"id": identifier, "name": f"sumire-repair-state-v1-{run}-1-{slot}",
                      "expired": False, "created_at": "2026-11-14T10:00:00Z",
                      "workflow_run": {"id": run, "repository_id": 99,
                                       "head_repository_id": 99, "head_branch": "dev"}}
            result.update(changes)
            return result

        artifacts = [artifact(1, 1, 0), artifact(2, 1, 1), artifact(3, 2, 0),
                     artifact(4, 1, 0, expired=True)]
        fork_artifact = artifact(5, 3, 0)
        fork_artifact["workflow_run"]["head_repository_id"] = 100
        artifacts.append(fork_artifact)

        def metadata(path):
            if "/artifacts?" in path:
                return {"artifacts": artifacts}
            return {"path": ".github/workflows/prepare-upstream-sync.yml", "head_repository": {
                "full_name": "ajctrl/Sumire"},
                "event": "workflow_dispatch" if path.endswith("/1") else "pull_request"}

        def download(command, **kwargs):
            identifier = int(command[-1].split("/")[-2])
            self.assertIn(identifier, (1, 2))
            return subprocess.CompletedProcess(command, 0, archive(snapshot(4 if identifier == 1 else 5)))

        with patch.object(state, "gh_json", side_effect=metadata), \
                patch.object(state.subprocess, "run", side_effect=download) as requests:
            restored = state.restore_remote()
        self.assertEqual(restored["revision"], 5)
        self.assertEqual(requests.call_count, 2)

    def test_legacy_or_validation_runs_cannot_supply_repair_checkpoints(self):
        for path, event in ((".github/workflows/sync-upstream.yml", "workflow_dispatch"),
                            (".github/workflows/sync-upstream.yml", "schedule"),
                            (".github/workflows/sync-upstream.yml", "workflow_run"),
                            (".github/workflows/fork-validation.yml", "pull_request")):
            artifact = {"id": 1, "name": "sumire-repair-state-v1-42-1-0", "expired": False,
                        "created_at": "2026-11-14T10:00:00Z", "workflow_run": {
                            "id": 42, "repository_id": 99, "head_repository_id": 99,
                            "head_branch": "dev"}}

            def metadata(request):
                if "/artifacts?" in request:
                    return {"artifacts": [artifact]}
                return {"path": path, "event": event,
                        "head_repository": {"full_name": "ajctrl/Sumire"}}

            with self.subTest(path=path, event=event), \
                    patch.object(state, "gh_json", side_effect=metadata), \
                    patch.object(state.subprocess, "run") as download:
                self.assertIsNone(state.restore_remote())
                download.assert_not_called()

    def test_invalid_checkpoint_does_not_silently_reset_usage(self):
        for bad in ([], {}, dict(snapshot(), used_tokens=-1), dict(snapshot(), pending={"id": -1}),
                    dict(snapshot(), cached_input_tokens=80, tokens_without_cache_details=30)):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                state.validate(bad)

    def test_publish_failure_prevents_further_work(self):
        current = state.RepairState()
        current.remote = True
        with patch.object(state.subprocess, "run", side_effect=RuntimeError("upload unavailable")):
            with self.assertRaisesRegex(RuntimeError, "upload unavailable"):
                current.reserve(9000, 10000)
        self.assertEqual(sum(json.loads(current.file.read_text())["pending"].values()), 9000)

    def test_checkpoint_slots_keep_last_good_state_during_interrupted_upload(self):
        sdk = self.root / "sdk.cjs"
        inventory = self.root / "artifacts.json"
        sdk.write_text('''
const fs = require('node:fs');
const file = process.env.TEST_ARTIFACT_INVENTORY;
const read = () => fs.existsSync(file) ? JSON.parse(fs.readFileSync(file)) : {};
class ArtifactNotFoundError extends Error {}
module.exports = {ArtifactNotFoundError, default: {
  async deleteArtifact(name) {
    const items = read();
    if (!items[name]) throw new ArtifactNotFoundError();
    delete items[name]; fs.writeFileSync(file, JSON.stringify(items));
  },
  async uploadArtifact(name, paths, root, options) {
    const snapshot = JSON.parse(fs.readFileSync(paths[0]));
    if (String(snapshot.revision) === process.env.TEST_FAIL_REVISION) throw new Error('interrupted');
    const items = read(); items[name] = snapshot;
    fs.writeFileSync(file, JSON.stringify(items)); return {id: snapshot.revision};
  }
}};
''')
        current = state.RepairState()
        current.remote = True
        with patch.dict(os.environ, SUMIRE_ARTIFACT_CLIENT=str(sdk), GITHUB_RUN_ID="42",
                        GITHUB_RUN_ATTEMPT="1", TEST_ARTIFACT_INVENTORY=str(inventory)):
            for _ in range(12):
                current.save()
                self.assertLessEqual(len(json.loads(inventory.read_text())), 2)
            with patch.dict(os.environ, TEST_FAIL_REVISION="13"):
                with self.assertRaises(subprocess.CalledProcessError):
                    current.save()
        remaining = list(json.loads(inventory.read_text()).values())
        self.assertEqual(max(item["revision"] for item in remaining), 12)


if __name__ == "__main__":
    unittest.main()
