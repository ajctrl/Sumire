"""Durable repair results and a UTC daily API allowance."""

from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
import zipfile


# This producer never executes update-branch code, including Gradle or tests.
TRUSTED_WORKFLOW = ".github/workflows/prepare-upstream-sync.yml"
ARTIFACT = re.compile(r"sumire-repair-state-v1-(\d+)-(\d+)-[01]$")


class BudgetPaused(RuntimeError):
    """Work is resumable; no further generation should start today."""


def today():
    return datetime.now(timezone.utc).date().isoformat()


def validate(state):
    if (not isinstance(state, dict) or state.get("version") != 1 or type(state.get("revision")) is not int
            or state["revision"] < 0 or not isinstance(state.get("date"), str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", state["date"])
            or type(state.get("used_tokens")) is not int or state["used_tokens"] < 0
            or not isinstance(state.get("pending"), dict)
            or any(type(n) is not int or n < 0 for n in state["pending"].values())
            or not isinstance(state.get("repairs"), dict)):
        raise ValueError("Invalid repair checkpoint")
    cached = state.get("cached_input_tokens", 0)
    unknown = state.get("tokens_without_cache_details", state["used_tokens"])
    if (type(cached) is not int or type(unknown) is not int
            or min(cached, unknown) < 0 or cached + unknown > state["used_tokens"]):
        raise ValueError("Invalid cache usage in repair checkpoint")
    return state


def gh_json(path):
    result = subprocess.run(["gh", "api", path], check=True, text=True, capture_output=True)
    return json.loads(result.stdout)


def restore_remote():
    """Only trust the dedicated dev preparation workflow, which runs trusted code."""
    repo = os.environ["GITHUB_REPOSITORY"]
    repo_id = os.environ["GITHUB_REPOSITORY_ID"]
    candidates = []
    page = 1
    while True:
        artifacts = gh_json(f"repos/{repo}/actions/artifacts?per_page=100&page={page}")["artifacts"]
        for artifact in artifacts:
            match = ARTIFACT.fullmatch(artifact["name"])
            run = artifact.get("workflow_run", {})
            if (match and not artifact["expired"] and str(run.get("repository_id")) == repo_id
                    and str(run.get("head_repository_id")) == repo_id
                    and run.get("head_branch") == "dev" and str(run.get("id")) == match[1]):
                candidates.append(artifact)
        if len(artifacts) < 100:
            break
        page += 1
    runs = {}
    restored = []
    newest = None
    for artifact in sorted(candidates, key=lambda a: a["created_at"], reverse=True):
        if newest is not None and artifact["created_at"] < newest:
            break
        run_id = artifact["workflow_run"]["id"]
        if run_id not in runs:
            runs[run_id] = gh_json(f"repos/{repo}/actions/runs/{run_id}")
        run = runs[run_id]
        if ((run.get("path") or "").split("@")[0] != TRUSTED_WORKFLOW
                or run.get("event") not in ("schedule", "workflow_dispatch")
                or (run.get("head_repository") or {}).get("full_name") != repo):
            continue
        result = subprocess.run(["gh", "api", f"repos/{repo}/actions/artifacts/{artifact['id']}/zip"],
                                check=True, capture_output=True)
        with zipfile.ZipFile(io.BytesIO(result.stdout)) as archive:
            entry = archive.getinfo("repair-state.json")
            if entry.file_size > 16_000_000:
                raise ValueError("Repair checkpoint is too large")
            restored.append(validate(json.loads(archive.read(entry))))
        newest = artifact["created_at"]
    return max(restored, key=lambda s: s["revision"]) if restored else None


class RepairState:
    def __init__(self, remote=False):
        self.file = Path(os.environ.get("OPENAI_BUDGET_FILE") or
                         str(Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "repair-state.json"))
        self.remote = remote and bool(os.environ.get("SUMIRE_ARTIFACT_CLIENT"))
        if self.file.exists():
            self.data = validate(json.loads(self.file.read_text()))
        else:
            restored = restore_remote() if self.remote else None
            self.data = restored or {"version": 1, "revision": 0, "date": today(),
                                     "used_tokens": 0, "pending": {}, "repairs": {}}
        # Old checkpoints carry gross usage only.
        self.data.setdefault("cached_input_tokens", 0)
        self.data.setdefault("tokens_without_cache_details", self.data["used_tokens"])

    def save(self):
        self.data["revision"] += 1
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.file.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False) + "\n")
        temporary.replace(self.file)
        if self.remote:
            helper = Path(__file__).with_name("save-repair-state.cjs")
            subprocess.run(["node", str(helper), str(self.file)], check=True)

    def remaining(self, limit):
        if self.data["date"] != today():
            self.data.update(date=today(), used_tokens=0, cached_input_tokens=0,
                             tokens_without_cache_details=0, pending={})
        spent = self.data["used_tokens"] + sum(self.data["pending"].values())
        return max(0, limit - spent)

    def reserve(self, tokens, limit):
        remaining = self.remaining(limit)
        if tokens > limit:
            raise RuntimeError("One response cannot fit the daily allowance; manual review required")
        if tokens > remaining:
            spent = limit - remaining
            raise BudgetPaused(f"Daily allowance reached ({spent}/{limit}); resume after UTC 00:00")
        reservation = uuid.uuid4().hex
        self.data["pending"][reservation] = tokens
        # Persist the worst-case charge before requesting generation. A lost
        # response or killed runner must not reset the same day's allowance.
        self.save()
        if self.data["date"] != today():
            raise BudgetPaused("UTC day changed while saving; resume on the next scheduled or manual run")
        return reservation

    def settle(self, reservation, response):
        reserved = self.data["pending"].pop(reservation)
        usage = response.get("usage") or {}
        actual = usage.get("total_tokens")
        input_tokens = usage.get("input_tokens")
        details = usage.get("input_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if type(actual) is not int or actual < 0:
            actual = reserved  # An unknown charge is never treated as zero.
            cached = None
        if (type(cached) is int and type(input_tokens) is int
                and 0 <= cached <= input_tokens <= actual):
            self.data["cached_input_tokens"] += cached
        else:
            cached = None
            self.data["tokens_without_cache_details"] += actual
        self.data["used_tokens"] += actual
        print(f"Accounted tokens: {actual}; cached input: {cached if cached is not None else 'unknown'}. "
              f"UTC day: {self.data['used_tokens']} total, {self.data['cached_input_tokens']} confirmed cached input, "
              f"{self.data['tokens_without_cache_details']} tokens without cache details.")

    def entry(self, task, path, content):
        key = hashlib.sha256(json.dumps([task, path, content], ensure_ascii=False).encode()).hexdigest()
        return key, self.data["repairs"].get(key, {})

    def remember(self, key, task, **values):
        self.data["repairs"][key] = dict(task=task, **values)
        self.save()

    def finish(self, task):
        self.data["repairs"] = {k: v for k, v in self.data["repairs"].items() if v.get("task") != task}
        self.save()
