"""Resume a failed validation in a fresh, daily-budgeted preparation run.

Only the trusted preparation artifact identifies the candidate. Validation logs
are untrusted diagnostic text, never instructions, executable code or a plan.
"""
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

TITLE = re.compile(r"Validate upstream ([1-9][0-9]*):([1-9][0-9]*)$")
spec = importlib.util.spec_from_file_location("repair_plan", Path(__file__).with_name("sync-plan.py"))
plans = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plans)


def api(path):
    return json.loads(raw(path))


def raw(path):
    # Logs can contain terminal escapes. Capture them as bytes, never render them.
    return subprocess.run(["gh", "api", path, "--allow-escape-sequences"],
                          check=True, capture_output=True).stdout


def trusted_run(run, repo, path, events):
    if (run.get("path", "").split("@")[0] != path or run.get("event") not in events
            or run.get("head_branch") != "dev"
            or (run.get("repository") or {}).get("full_name") != repo
            or (run.get("head_repository") or {}).get("full_name") != repo
            or run.get("status") != "completed"):
        raise ValueError("Untrusted or incomplete workflow run")


def failure(repo, run_id):
    if not re.fullmatch(r"[1-9][0-9]*", str(run_id)):
        raise ValueError("Invalid validation run ID")
    run = api(f"repos/{repo}/actions/runs/{run_id}")
    trusted_run(run, repo, ".github/workflows/sync-upstream.yml", {"workflow_run"})
    if run["conclusion"] not in ("failure", "timed_out"):
        return None
    match = TITLE.fullmatch(run.get("display_title", ""))
    if not match:
        raise ValueError("Validation run has no trusted preparation identity")
    producer, attempt = match.groups()
    source = api(f"repos/{repo}/actions/runs/{producer}/attempts/{attempt}")
    trusted_run(source, repo, ".github/workflows/prepare-upstream-sync.yml",
                {"schedule", "workflow_dispatch"})
    if source["conclusion"] != "success" or source["run_attempt"] != int(attempt):
        raise ValueError("Preparation did not finish successfully")
    artifacts = []
    page = 1
    while True:
        batch = api(f"repos/{repo}/actions/runs/{producer}/artifacts?per_page=100&page={page}")["artifacts"]
        artifacts.extend(a for a in batch if a["name"] == f"sumire-sync-plan-{producer}-{attempt}")
        if len(batch) < 100:
            break
        page += 1
    if len(artifacts) != 1 or artifacts[0]["expired"]:
        raise ValueError("Trusted preparation artifact is unavailable")
    with zipfile.ZipFile(io.BytesIO(raw(f"repos/{repo}/actions/artifacts/{artifacts[0]['id']}/zip"))) as archive:
        if archive.getinfo("sync-plan.json").file_size > 16_384:
            raise ValueError("Preparation artifact is too large")
        plan = plans.validate(json.loads(archive.read("sync-plan.json")), repo, producer, attempt)
    if not plan["changed"] or plan["review_required"] or plan["paused"]:
        return None
    if source["head_sha"] != plan["base"] or run["head_sha"] != plan["base"]:
        return None  # The default branch moved before validation started.
    jobs = []
    page = 1
    while True:
        batch = api(f"repos/{repo}/actions/runs/{run_id}/attempts/{run['run_attempt']}/jobs?per_page=100&page={page}")["jobs"]
        jobs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    if not any(j["name"] == "prepare" and j["conclusion"] == "success" for j in jobs):
        return None
    failed = [j for j in jobs if j["name"].startswith(("validate /", "mushroom /", "zenz /"))
              and j["conclusion"] in ("failure", "timed_out")]
    if not failed:
        return None  # Merge, signing, publication and infrastructure setup are not code repairs.
    evidence = []
    for job in failed:
        log = raw(f"repos/{repo}/actions/jobs/{job['id']}/logs").decode("utf-8", errors="replace")
        log = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", log)
        # Bound context while retaining both setup information and final errors.
        if len(log) > 48_000:
            log = log[:8_000] + "\n[log truncated; full log linked below]\n" + log[-40_000:]
        evidence.append({"job": job["name"], "url": job["html_url"], "log": log})
    return plan, evidence


def candidate_depth(sync, base, upstream, head, next_state):
    """Verify every repair back to the original merge, rejecting repeated trees."""
    current, depth = head, 0
    seen = set()
    while True:
        tree = sync.git("rev-parse", current + "^{tree}")
        if tree in seen:
            raise sync.conflict_resolver.policy.ReviewRequired("Validation repairs repeat a previous candidate tree")
        seen.add(tree)
        message = sync.git("show", "-s", "--format=%B", current).splitlines()
        if (f"Sumire-Sync-Tree: {sync.git('rev-parse', current + '^{tree}')}" not in message
                or f"Sumire-Conflict-Policy: {sync.conflict_resolver.policy.VERSION}" not in message
                or json.loads(sync.git("show", f"{current}:{sync.STATE}")) != next_state
                or sync.git("diff", base, current, "--", ".github", ":(exclude).github/upstream.json")):
            raise sync.conflict_resolver.policy.ReviewRequired("Existing update branch was modified")
        parents = sync.git("show", "-s", "--format=%P", current).split()
        if parents == [base, upstream]:
            return depth
        if len(parents) != 1 or f"Sumire-Repair-Parent: {parents[0]}" not in message:
            break
        current = parents[0]
        depth += 1
    raise sync.conflict_resolver.policy.ReviewRequired("Invalid validation repair history")


def pending_key(base, head):
    return f"validation:{base}:{head}"


def resume(sync, run_id, context):
    repo = os.environ["GITHUB_REPOSITORY"]
    if repo != "ajctrl/Sumire":
        raise ValueError("Unexpected repository")
    if sync.git("status", "--porcelain"):
        raise RuntimeError("The working tree must be clean")
    sync.git("fetch", "--no-tags", "origin", "dev")
    base = sync.git("rev-parse", "origin/dev")
    result = failure(repo, run_id)
    if result is None:
        sync.output(changed="false", paused="true", base=base)
        print("No eligible failed validation; no repair, merge or publication started")
        return
    plan, evidence = result
    pull = sync.api(f"repos/{repo}/pulls/{plan['pr']}")
    if (base != plan["base"] or sync.git("rev-parse", "HEAD") != base
            or pull["state"] != "open" or pull["draft"] or pull["base"]["ref"] != "dev"
            or pull["head"]["repo"]["full_name"] != repo or pull["head"]["sha"] != plan["head"]):
        sync.output(changed="false", paused="true", base=base)
        print("Stale validation result; candidate and dev were not changed")
        return
    head = plan["head"]
    branch = pull["head"]["ref"]
    sync.git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
    if sync.git("rev-parse", "FETCH_HEAD") != head:
        raise RuntimeError("Candidate changed while loading validation")
    next_state = json.loads(sync.git("show", f"{head}:{sync.STATE}"))
    upstream, tag = next_state["commit"], next_state["tag"]
    sync.version(tag)
    if next_state["repository"] != sync.UPSTREAM or branch != f"upstream/{tag}-{base[:12]}":
        raise ValueError("Unexpected candidate identity")
    context.update(repo=repo, base=base, upstream=upstream, tag=tag,
                   patch=sync.git("diff", "--no-ext-diff", base, head))
    existing = sync.upstream_review.find(repo, base, upstream)
    if existing:
        sync.upstream_review.evidence(existing, base, upstream, existing.get("body", ""))
        sync.review_outcome(context, existing["html_url"])
        return
    depth = candidate_depth(sync, base, upstream, head, next_state)
    url = f"https://github.com/{repo}/actions/runs/{run_id}"
    directory = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "upstream-review"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "validation-failure.json").write_text(json.dumps(
        {"run": url, "plan": plan, "failures": evidence}, ensure_ascii=False, indent=2))
    resolver = sync.conflict_resolver.Resolver(task=f"{repo}:{base}:{upstream}:validation:{head}")
    key = pending_key(base, head)
    resolver.state.remember(key, resolver.task, failed_run=str(run_id))
    sync.git("switch", "--detach", head)
    try:
        overview = {"base": base, "upstream": upstream, "candidate": head,
                    "repair_attempt": depth + 1, "validation_run": url,
                    "failures": evidence,
                    "repair_history": sync.git("log", "--format=%h %s", f"{base}..{head}", "--first-parent"),
                    "diff": sync.git("diff", "--no-ext-diff", base, head)}
        try:
            changed = resolver.repair_update([], overview=overview)
        except sync.conflict_resolver.BudgetPaused:
            raise
        except (RuntimeError, ValueError) as error:
            raise sync.conflict_resolver.policy.ReviewRequired(f"Validation repair stopped: {error}") from error
        if not changed or (not sync.git("diff", "--stat") and not sync.git("ls-files", "--others", "--exclude-standard")):
            raise sync.conflict_resolver.policy.ReviewRequired(f"Repair made no changes. Logs: {url}")
        sync.git("add", "--all", "--", *changed)
        if sync.git("diff", "--cached", "--", ".github"):
            raise sync.conflict_resolver.policy.ReviewRequired("Validation repair changes automation")
        if Path(sync.STATE).read_text() != sync.git("show", f"{head}:{sync.STATE}") + "\n":
            raise sync.conflict_resolver.policy.ReviewRequired("Validation repair changed upstream identity")
        if sync.release_versions.version_info(Path(sync.VERSION_FILE).read_text()) != sync.release_versions.version_info(
                sync.git("show", f"{head}:{sync.VERSION_FILE}")):
            raise sync.conflict_resolver.policy.ReviewRequired("Validation repair changed release version numbers")
        previous_trees = set(sync.git("log", "--first-parent", "--format=%T", f"{base}..{head}").splitlines())
        if sync.git("write-tree") in previous_trees:
            raise sync.conflict_resolver.policy.ReviewRequired("Repair repeats a previous candidate tree")
        sync.run("python3", ".github/scripts/check-fork-invariants.py")
        resolver.assess_update(sync.git("diff", "--no-ext-diff", base, "--"))
        sync.git("config", "user.name", "github-actions[bot]")
        sync.git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        sync.git("commit", "-m", f"Repair upstream {tag} validation (repair {depth + 1})", "-m",
                 f"Sumire-Sync-Tree: {sync.git('write-tree')}\n"
                 f"Sumire-Conflict-Policy: {sync.conflict_resolver.policy.VERSION}\n"
                 f"Sumire-Repair-Parent: {head}\nValidation-Run: {url}")
        repaired = sync.git("rev-parse", "HEAD")
        # Recheck base and PR, then require an ordinary fast-forward push.
        latest = sync.api(f"repos/{repo}/pulls/{plan['pr']}")
        if (sync.api(f"repos/{repo}/git/ref/heads/dev")["object"]["sha"] != base
                or latest["head"]["sha"] != head or latest["state"] != "open" or latest["draft"]):
            raise RuntimeError("Candidate or dev changed during repair; refusing to push")
        sync.git("push", "origin", f"HEAD:refs/heads/{branch}")
        resolver.finish()
        sync.output(changed="true", paused="false", base=base, head=repaired,
                    pr=plan["pr"], full_validation="true")
    except sync.conflict_resolver.BudgetPaused as error:
        sync.output(changed="false", paused="true", base=base)
        print(f"::notice::Validation repair paused; next scheduled run resumes it: {error}")
    except sync.conflict_resolver.policy.ReviewRequired as error:
        raise sync.conflict_resolver.policy.ReviewRequired(f"{error}\nValidation: {url}\nCandidate: {head}") from error
