"""Merge the latest stable upstream release, preserving Git ancestry and fork changes."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace


UPSTREAM = "KazumaProject/JapaneseKeyboard"
BASE = "dev"
STATE = Path(".github/upstream.json")
VERSION_FILE = "app/build.gradle"
PROTECTED = (".github",)
spec = importlib.util.spec_from_file_location(
    "release_versions", Path(__file__).with_name("release-upstream.py"))
release_versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_versions)
spec = importlib.util.spec_from_file_location(
    "conflict_resolver", Path(__file__).with_name("codex-resolver.py"))
conflict_resolver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conflict_resolver)

spec = importlib.util.spec_from_file_location("upstream_review", Path(__file__).with_name("upstream-review.py"))
upstream_review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upstream_review)
spec = importlib.util.spec_from_file_location("validation_repair", Path(__file__).with_name("validation-repair.py"))
validation_repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation_repair)


def run(*args, check=True):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check)


def git(*args):
    return run("git", *args).stdout.strip()


def api(path):
    return json.loads(run("gh", "api", path).stdout)


def version(tag):
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", tag)
    if not match:
        raise ValueError(f"Not a stable version tag: {tag!r}")
    return tuple(map(int, match.groups()))


def release_tag(release, current):
    if release["draft"] or release["prerelease"]:
        raise ValueError("Expected a published stable release")
    tag = release["tag_name"]
    return tag if version(tag) > version(current) else None


def output(**values):
    values = {"review_required": "false", "full_validation": "false", **values}
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def release_merge_base(previous, upstream, base):
    """Use normal Git ancestry unless the new release is on a sibling line."""
    git("merge-base", "--is-ancestor", previous, base)
    common = run("git", "merge-base", previous, upstream, check=False)
    if common.returncode == 1:
        raise conflict_resolver.policy.ReviewRequired("Upstream releases have no common history")
    common.check_returncode()
    included = run("git", "merge-base", "--is-ancestor", upstream, base, check=False)
    if included.returncode == 0:
        raise conflict_resolver.policy.ReviewRequired("Upstream release is already in dev history")
    if included.returncode != 1:
        included.check_returncode()
    return None if common.stdout.strip() == previous else previous


def merge_app_version(base, upstream, tag, previous_tag, explicit_base=None):
    """Merge real Gradle changes normally, handling only release numbers specially."""
    ancestor = explicit_base or git("merge-base", base, upstream)
    original = git("show", f"{ancestor}:{VERSION_FILE}")
    ours = git("show", f"{base}:{VERSION_FILE}")
    theirs = git("show", f"{upstream}:{VERSION_FILE}")
    old_name, old_code = release_versions.version_info(original)
    fork_name, fork_code = release_versions.version_info(ours)
    upstream_name, upstream_code = release_versions.version_info(theirs)
    release_versions.assert_version_base(fork_name, previous_tag)
    if tag != f"v{upstream_name}":
        raise ValueError("Upstream tag and application version do not match")
    # Remove only our version-number edits before doing a three-way merge.
    # Non-version conflicts are passed to the resolver when the API is enabled.
    ours = release_versions.replace_version_info(ours, old_name, old_code)
    with tempfile.TemporaryDirectory() as directory:
        paths = [Path(directory) / name for name in ("ours", "base", "theirs")]
        for path, content in zip(paths, (ours, original, theirs)):
            path.write_text(content + "\n")
        merged = run("git", "merge-file", "--diff3", "-L", "fork", "-L", "merge-base", "-L", "upstream", "-p", *map(str, paths), check=False)
    if merged.returncode:
        if 0 < merged.returncode < 128:
            return merged.stdout
        raise RuntimeError("Manual upstream merge required: app/build.gradle has non-version conflicts")
    return release_versions.replace_version_info(
        merged.stdout, upstream_name, max(upstream_code, fork_code + 1))


def merge_upstream(upstream, explicit_base=None):
    if explicit_base:
        # Set up an abortable merge with the real upstream parent. Then merge
        # its tree into the untouched index using the recorded release as base.
        git("merge", "--no-ff", "--no-commit", "-s", "ours", upstream)
        merged = run("git", "-c", "merge.conflictStyle=diff3", "merge-recursive",
                     explicit_base, "--", "HEAD", upstream, check=False)
    else:
        merged = run("git", "-c", "merge.conflictStyle=diff3", "merge",
                     "--no-ff", "--no-commit", upstream, check=False)
    if merged.returncode not in (0, 1):
        merged.check_returncode()
    return merged, git("diff", "--name-only", "--diff-filter=U").splitlines()


def prepare_conflicts(conflicts):
    """Classify ALL files before spending any tokens or applying any repair."""
    prepared = []
    for path in conflicts:
        file = Path(path)
        modes = git("ls-files", "--stage", "--", path).splitlines()
        if (len(modes) not in (2, 3) or
                {line.split()[2] for line in modes} not in ({"1", "2", "3"}, {"2", "3"}) or
                any(line.split()[0] not in ("100644", "100755") for line in modes)
                or not file.is_file() or file.is_symlink()):
            raise conflict_resolver.policy.ReviewRequired(f"Unsupported conflict type: {path}")
        content = file.read_text()
        conflict_resolver.policy.check_conflict(path, content)
        prepared.append((path, content))
    return prepared


def prepare(context):
    repo = os.environ["GITHUB_REPOSITORY"]
    if repo != "ajctrl/Sumire":
        raise ValueError("This workflow is configured for ajctrl/Sumire")
    state = json.loads(STATE.read_text())
    if state["repository"] != UPSTREAM:
        raise ValueError("Unexpected upstream repository")
    if git("status", "--porcelain"):
        raise RuntimeError("The working tree must be clean")
    git("fetch", "--no-tags", "origin", BASE)
    base = git("rev-parse", f"origin/{BASE}")
    if git("rev-parse", "HEAD") != base:
        raise RuntimeError("dev changed after checkout; rerun the workflow")
    tag = release_tag(api(f"repos/{UPSTREAM}/releases/latest"), state["tag"])
    if tag is None:
        output(changed="false", paused="false", base=base)
        print(f"Already at {state['tag']} or newer")
        return
    git("fetch", "--no-tags", f"https://github.com/{UPSTREAM}.git",
        f"refs/tags/{tag}")
    upstream = git("rev-parse", "FETCH_HEAD^{commit}")
    context.update(repo=repo, base=base, upstream=upstream, tag=tag)
    pending = upstream_review.find(repo, base, upstream)
    if pending:
        upstream_review.evidence(pending, base, upstream, pending.get("body") or "Review is pending")
        review_outcome(context, pending["html_url"])
        return
    task = f"{repo}:{base}:{upstream}"
    resolver = None
    explicit_base = release_merge_base(state["commit"], upstream, base)
    branch = f"upstream/{tag}-{base[:12]}"
    next_state = {"repository": UPSTREAM, "tag": tag, "commit": upstream}
    existing = git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if existing:
        # Reuse the exact candidate: AI output can differ between requests.
        # Validate its parents, checkpoint and trusted automation before checkout.
        git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
        previous = git("rev-parse", "FETCH_HEAD")
        validation_repair.candidate_depth(SimpleNamespace(**globals()), base, upstream, previous, next_state)
        resolver = conflict_resolver.Resolver(task=task)
        pending = resolver.state.data["repairs"].get(validation_repair.pending_key(base, previous))
        if pending:
            validation_repair.resume(SimpleNamespace(**globals()), pending["failed_run"], context)
            return
        git("switch", "-c", branch, previous)
        run("python3", ".github/scripts/check-fork-invariants.py")
        head = previous
    else:
        merged_gradle = merge_app_version(base, upstream, tag, state["tag"],
                                          explicit_base=explicit_base)
        git("switch", "-c", branch)
        git("config", "user.name", "github-actions[bot]")
        git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        try:
            merged, conflicts = merge_upstream(upstream, explicit_base)
            if merged.returncode and not conflicts:
                raise RuntimeError(f"Upstream merge failed: {merged.stderr}")
            # Do not hide upstream policy changes by silently retaining ours.
            if any(p.startswith(".github/") for p in conflicts) or git("diff", "HEAD", "--", *PROTECTED):
                raise conflict_resolver.policy.ReviewRequired("Update changes fork automation; manual review required")
            Path(VERSION_FILE).write_text(merged_gradle)
            conflicts = [p for p in conflicts if p != VERSION_FILE]
            if conflict_resolver.policy.HUNK.search(merged_gradle):
                conflicts.append(VERSION_FILE)
            else:
                git("add", "--", VERSION_FILE)
            prepared = prepare_conflicts(conflicts)
            if prepared:
                resolver = conflict_resolver.Resolver(task=task)
                if not resolver.enabled:
                    raise RuntimeError(f"Manual upstream merge required:\n{conflicts}")
                overview = {"base": base, "upstream": upstream,
                            "paths": git("diff", "--name-status", "HEAD", "--"),
                            "statistics": git("diff", "--stat", "HEAD", "--"),
                            "diff": git("diff", "--no-ext-diff", "HEAD", "--")}
                changed = resolver.repair_update(prepared, overview=overview)
                _, fork_code = release_versions.version_info(git("show", f"{base}:{VERSION_FILE}"))
                name, upstream_code = release_versions.version_info(git("show", f"{upstream}:{VERSION_FILE}"))
                Path(VERSION_FILE).write_text(release_versions.replace_version_info(
                    Path(VERSION_FILE).read_text(), name, max(upstream_code, fork_code + 1)))
                git("add", "--all", "--", *changed, VERSION_FILE)
            if git("diff", "HEAD", "--", *PROTECTED):
                raise conflict_resolver.policy.ReviewRequired("Update changes fork automation; manual review required")
            run("python3", ".github/scripts/check-fork-invariants.py")
            if resolver is not None:
                resolver.assess_update(git("diff", "--no-ext-diff", "HEAD", "--"))
            STATE.write_text(json.dumps(next_state, indent=2) + "\n")
            git("add", "--", str(STATE))
            git("commit", "-m", f"Merge upstream {tag}", "-m",
                f"Sumire-Sync-Tree: {git('write-tree')}\n"
                f"Sumire-Conflict-Policy: {conflict_resolver.policy.VERSION}")
        except conflict_resolver.BudgetPaused as error:
            if run("git", "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
                git("merge", "--abort")
            output(changed="false", paused="true", base=base)
            print(f"::notice::Repair paused: {error}")
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
                    summary.write("## Repair paused\n\nThe daily API allowance is reached. "
                                  "Saved repairs will be reused on the next scheduled or manual run. "
                                  "No merge or release was started.\n")
            return
        except Exception as error:
            if isinstance(error, conflict_resolver.policy.ReviewRequired):
                context["patch"] = git("diff", "--no-ext-diff", "HEAD", "--")
            if run("git", "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
                git("merge", "--abort")
            raise
        head = git("rev-parse", "HEAD")
        git("push", "origin", f"HEAD:refs/heads/{branch}")
    if resolver is not None:
        resolver.finish()
    pulls = api(f"repos/{repo}/pulls?state=all&base={BASE}&head={repo.split('/')[0]}:{branch}")
    if pulls:
        if len(pulls) != 1 or pulls[0]["state"] != "open":
            raise RuntimeError("Update PR was closed; review it before retrying")
        pr = pulls[0]["number"]
    else:
        body = (f"## Upstream update\n\nMerge [{tag}](https://github.com/{UPSTREAM}/releases/tag/{tag}) "
                f"into `{BASE}`, preserving the clipboard sync bridge and fork application ID.\n\n"
                f"Upstream: `{upstream}`\nBase: `{base}`\n\n"
                "The Validate and publish upstream update workflow runs the Lite debug build, all app unit tests, "
                "Lint and fork invariant checks before merging this exact revision. "
                "Full debug build, unit tests, Lint, dictionary checks, Mushroom device regression and all Zenz "
                "performance phases also run before merging. Failed validation starts a separate bounded repair run.\n\n"
                "Use a merge commit to preserve upstream ancestry.\n")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md") as file:
            file.write(body)
            file.flush()
            run("gh", "pr", "create", "--repo", repo, "--base", BASE, "--head", branch,
                "--title", f"Merge upstream {tag}", "--body-file", file.name)
        pr = api(f"repos/{repo}/pulls?state=open&base={BASE}&head={repo.split('/')[0]}:{branch}")[0]["number"]
    output(changed="true", paused="false", head=head, base=base, pr=pr, full_validation="true")
    print(f"Prepared https://github.com/{repo}/pull/{pr} at {head}")


def review_outcome(context, url):
    output(changed="false", paused="false", review_required="true", full_validation="false", base=context["base"])
    print(f"::notice::Upstream update awaits human review: {url}")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
            summary.write(f"## Human review required\n\n{url}\n\nNo merge or release was started.\n")


def main():
    context = {}
    try:
        failed_run = os.environ.get("SUMIRE_FAILED_VALIDATION_RUN")
        if failed_run:
            validation_repair.resume(SimpleNamespace(**globals()), failed_run, context)
        else:
            prepare(context)
    except conflict_resolver.policy.ReviewRequired as error:
        if "upstream" not in context:
            raise
        issue = upstream_review.record(context["repo"], context["base"], context["upstream"],
                                       context["tag"], str(error), context.get("patch", ""))
        review_outcome(context, issue["html_url"])


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Command failed: {error.cmd}\n{error.stderr}") from error
