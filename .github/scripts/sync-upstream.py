"""Merge the latest stable upstream release, preserving Git ancestry and fork changes."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


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
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def assert_history(previous, upstream, base):
    git("merge-base", "--is-ancestor", previous, base)
    result = run("git", "merge-base", "--is-ancestor", previous, upstream, check=False)
    if result.returncode:
        raise RuntimeError("Manual review required: upstream release history diverged; API repair is not allowed")


def merge_app_version(base, upstream, tag, previous_tag, allow_conflicts=False):
    """Merge real Gradle changes normally, handling only release numbers specially."""
    ancestor = git("merge-base", base, upstream)
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
        if allow_conflicts and 0 < merged.returncode < 128:
            return merged.stdout
        raise RuntimeError("Manual upstream merge required: app/build.gradle has non-version conflicts")
    return release_versions.replace_version_info(
        merged.stdout, upstream_name, max(upstream_code, fork_code + 1))


def merge_upstream(upstream):
    merged = run("git", "-c", "merge.conflictStyle=diff3", "merge",
                 "--no-ff", "--no-commit", upstream, check=False)
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
            raise RuntimeError(f"Manual review required: unsupported conflict type: {path}")
        content = file.read_text()
        conflict_resolver.policy.check_conflict(path, content)
        prepared.append((path, content))
    return prepared


def main():
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
    resolver = conflict_resolver.Resolver(task=f"{repo}:{base}:{upstream}")
    assert_history(state["commit"], upstream, base)
    branch = f"upstream/{tag}-{base[:12]}"
    next_state = {"repository": UPSTREAM, "tag": tag, "commit": upstream}
    existing = git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if existing:
        # Reuse the exact candidate: AI output can differ between requests.
        # Validate its parents, checkpoint and trusted automation before checkout.
        git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
        previous = git("rev-parse", "FETCH_HEAD")
        if (git("show", "-s", "--format=%P", previous) != f"{base} {upstream}"
                or f"Sumire-Sync-Tree: {git('rev-parse', previous + '^{tree}')}" not in
                git("show", "-s", "--format=%B", previous).splitlines()
                or f"Sumire-Conflict-Policy: {conflict_resolver.policy.VERSION}" not in
                git("show", "-s", "--format=%B", previous).splitlines()
                or json.loads(git("show", f"{previous}:{STATE}")) != next_state
                or git("diff", base, previous, "--", ".github", ":(exclude).github/upstream.json")):
            raise RuntimeError("Existing update branch was modified; manual review required")
        git("switch", "-c", branch, previous)
        run("python3", ".github/scripts/check-fork-invariants.py")
        head = previous
    else:
        merged_gradle = merge_app_version(base, upstream, tag, state["tag"], allow_conflicts=resolver.enabled)
        git("switch", "-c", branch)
        git("config", "user.name", "github-actions[bot]")
        git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        try:
            merged, conflicts = merge_upstream(upstream)
            if merged.returncode and not conflicts:
                raise RuntimeError(f"Upstream merge failed: {merged.stderr}")
            # Do not hide upstream policy changes by silently retaining ours.
            if any(p.startswith(".github/") for p in conflicts) or git("diff", "HEAD", "--", *PROTECTED):
                raise RuntimeError("Update changes fork automation; manual review required")
            Path(VERSION_FILE).write_text(merged_gradle)
            conflicts = [p for p in conflicts if p != VERSION_FILE]
            if conflict_resolver.policy.HUNK.search(merged_gradle):
                conflicts.append(VERSION_FILE)
            else:
                git("add", "--", VERSION_FILE)
            if conflicts and not resolver.enabled:
                raise RuntimeError(f"Manual upstream merge required:\n{conflicts}")
            prepared = prepare_conflicts(conflicts)
            if prepared:
                changed = resolver.repair_update(prepared)
                _, fork_code = release_versions.version_info(git("show", f"{base}:{VERSION_FILE}"))
                name, upstream_code = release_versions.version_info(git("show", f"{upstream}:{VERSION_FILE}"))
                Path(VERSION_FILE).write_text(release_versions.replace_version_info(
                    Path(VERSION_FILE).read_text(), name, max(upstream_code, fork_code + 1)))
                git("add", "--all", "--", *changed, VERSION_FILE)
            if git("diff", "HEAD", "--", *PROTECTED):
                raise RuntimeError("Update changes fork automation; manual review required")
            run("python3", ".github/scripts/check-fork-invariants.py")
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
            if isinstance(error, conflict_resolver.policy.ReviewRequired) and os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as summary:
                    summary.write("## Human review required\n\n" + str(error) + "\n\nNo merge or release was started.\n")
            if run("git", "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
                git("merge", "--abort")
            raise
        head = git("rev-parse", "HEAD")
        git("push", "origin", f"HEAD:refs/heads/{branch}")
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
                "Full/native builds and device regression tests are not covered.\n\n"
                "Use a merge commit to preserve upstream ancestry.\n")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md") as file:
            file.write(body)
            file.flush()
            run("gh", "pr", "create", "--repo", repo, "--base", BASE, "--head", branch,
                "--title", f"Merge upstream {tag}", "--body-file", file.name)
        pr = api(f"repos/{repo}/pulls?state=open&base={BASE}&head={repo.split('/')[0]}:{branch}")[0]["number"]
    output(changed="true", paused="false", head=head, base=base, pr=pr)
    print(f"Prepared https://github.com/{repo}/pull/{pr} at {head}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Command failed: {error.cmd}\n{error.stderr}") from error
