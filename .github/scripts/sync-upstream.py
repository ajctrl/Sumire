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
# A reviewed transition from the old release history to the new v1.7.150
# history. Require the exact old checkpoint and a descendant of this new one.
HISTORY_TRANSITIONS = {
    "8528261c64dee373c922c010165d936cddc89640":
        "b53f9f7133af3245b3d3ecec53784e19d38c7d7d",
}
PROTECTED = (".github",)
spec = importlib.util.spec_from_file_location(
    "release_versions", Path(__file__).with_name("release-upstream.py"))
release_versions = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_versions)
spec = importlib.util.spec_from_file_location(
    "conflict_resolver", Path(__file__).with_name("resolve-upstream-conflicts.py"))
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


def assert_history(previous, upstream, base, resolver):
    git("merge-base", "--is-ancestor", previous, base)
    result = run("git", "merge-base", "--is-ancestor", previous, upstream, check=False)
    if result.returncode == 0:
        return
    checkpoint = HISTORY_TRANSITIONS.get(previous)
    if result.returncode != 1 or not resolver.enabled or not checkpoint:
        raise RuntimeError("Upstream release history diverged; an explicit history transition is required")
    git("merge-base", "--is-ancestor", checkpoint, upstream)
    print(f"Reconciling reviewed upstream history transition: {previous} -> {checkpoint}")
    return previous


def merge_app_version(base, upstream, tag, previous_tag, resolver=None, checkpoint=None):
    """Merge real Gradle changes normally, handling only release numbers specially."""
    ancestor = checkpoint or git("merge-base", base, upstream)
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
    # Conflicts in application ID, signing, dependencies, etc. still stop the update.
    ours = release_versions.replace_version_info(ours, old_name, old_code)
    with tempfile.TemporaryDirectory() as directory:
        paths = [Path(directory) / name for name in ("ours", "base", "theirs")]
        for path, content in zip(paths, (ours, original, theirs)):
            path.write_text(content + "\n")
        merged = run("git", "merge-file", "--diff3", "-p", *map(str, paths), check=False)
    if merged.returncode:
        if merged.returncode > 0 and merged.returncode < 128 and resolver and resolver.enabled:
            content = resolver.resolve(VERSION_FILE, merged.stdout)
        else:
            raise RuntimeError("Manual upstream merge required: app/build.gradle has non-version conflicts")
    else:
        content = merged.stdout
    return release_versions.replace_version_info(
        content, upstream_name, max(upstream_code, fork_code + 1))


def merge_upstream(base, upstream, checkpoint=None):
    if checkpoint is None:
        merged = run("git", "-c", "merge.conflictStyle=diff3", "merge",
                     "--no-ff", "--no-commit", upstream, check=False)
        return merged, git("diff", "--name-only", "--diff-filter=U").splitlines()
    # Rewritten history shares an ancient Git ancestor. Use the exact upstream
    # snapshot already incorporated into this fork as the three-way content
    # base, rather than asking the model to merge years of duplicate history.
    # The final commit still has the real fork and upstream commits as parents.
    tree = run("git", "-c", "merge.conflictStyle=diff3", "merge-tree", "--write-tree",
               f"--merge-base={checkpoint}", "--name-only", "--no-messages",
               base, upstream, check=False)
    if tree.returncode not in (0, 1):
        raise RuntimeError(f"Cannot reconcile upstream history: {tree.stderr}")
    lines = tree.stdout.splitlines()
    if not lines or not re.fullmatch(r"[0-9a-f]{40}", lines[0]):
        raise RuntimeError("Cannot identify reconciled upstream tree")
    merged = run("git", "merge", "--strategy=ours", "--allow-unrelated-histories",
                 "--no-ff", "--no-commit", upstream,
                 check=False)
    if merged.returncode:
        raise RuntimeError(f"Cannot establish upstream merge parents: {merged.stderr}")
    git("restore", f"--source={lines[0]}", "--staged", "--worktree", "--", ".")
    return merged, [line for line in lines[1:] if line]


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
        output(changed="false", base=base)
        print(f"Already at {state['tag']} or newer")
        return
    git("fetch", "--no-tags", f"https://github.com/{UPSTREAM}.git",
        f"refs/tags/{tag}")
    upstream = git("rev-parse", "FETCH_HEAD^{commit}")
    resolver = conflict_resolver.Resolver()
    checkpoint = assert_history(state["commit"], upstream, base, resolver)
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
                or json.loads(git("show", f"{previous}:{STATE}")) != next_state
                or git("diff", base, previous, "--", ".github", ":(exclude).github/upstream.json")):
            raise RuntimeError("Existing update branch was modified; manual review required")
        git("switch", "-c", branch, previous)
        run("python3", ".github/scripts/check-fork-invariants.py")
        head = previous
    else:
        merged_gradle = merge_app_version(base, upstream, tag, state["tag"], resolver, checkpoint)
        git("switch", "-c", branch)
        git("config", "user.name", "github-actions[bot]")
        git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
        try:
            merged, conflicts = merge_upstream(base, upstream, checkpoint)
            if merged.returncode and not conflicts:
                raise RuntimeError(f"Upstream merge failed: {merged.stderr}")
            # The model never edits approval, validation or release automation.
            # In API mode keep this fork's entire .github tree, including paths
            # added/deleted upstream. This also resolves automation conflicts.
            if resolver.enabled:
                git("restore", f"--source={base}", "--staged", "--worktree", "--", *PROTECTED)
            elif any(p.startswith(".github/") for p in conflicts):
                raise RuntimeError(f"Update changes fork automation; manual review required: {conflicts}")
            Path(VERSION_FILE).write_text(merged_gradle)
            git("add", "--", VERSION_FILE)
            conflicts = [p for p in conflicts if p != VERSION_FILE and
                         not (resolver.enabled and p.startswith(".github/"))]
            if conflicts and not resolver.enabled:
                raise RuntimeError(f"Manual upstream merge required:\n{conflicts}")
            for path in conflicts:
                file = Path(path)
                modes = git("ls-files", "--stage", "--", path).splitlines()
                if (not modes or any(line.split()[0] != "100644" for line in modes)
                        or not file.is_file() or file.is_symlink()):
                    raise RuntimeError(f"Unsupported conflict type: {path}")
                file.write_text(resolver.resolve(path, file.read_text()))
                git("add", "--", path)
            if git("diff", "HEAD", "--", *PROTECTED):
                raise RuntimeError("Update changes fork automation; manual review required")
            run("python3", ".github/scripts/check-fork-invariants.py")
            STATE.write_text(json.dumps(next_state, indent=2) + "\n")
            git("add", "--", str(STATE))
            git("commit", "-m", f"Merge upstream {tag}", "-m",
                f"Sumire-Sync-Tree: {git('write-tree')}")
        except Exception:
            if run("git", "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode == 0:
                git("merge", "--abort")
            raise
        head = git("rev-parse", "HEAD")
        git("push", "origin", f"HEAD:refs/heads/{branch}")
    pulls = api(f"repos/{repo}/pulls?state=all&base={BASE}&head={repo.split('/')[0]}:{branch}")
    if pulls:
        if len(pulls) != 1 or pulls[0]["state"] != "open":
            raise RuntimeError("Update PR was closed; review it before retrying")
        pr = pulls[0]["number"]
    else:
        body = (f"## Upstream update\n\nMerge [{tag}](https://github.com/{UPSTREAM}/releases/tag/{tag}) "
                f"into `{BASE}`, preserving the clipboard sync bridge and fork application ID.\n\n"
                f"Upstream: `{upstream}`\nBase: `{base}`\n\n"
                "The Sync upstream release workflow runs the Lite debug build, all app unit tests, "
                "Lint and fork invariant checks before merging this exact revision. "
                "Full/native builds and device regression tests are not covered.\n\n"
                "Use a merge commit to preserve upstream ancestry.\n")
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md") as file:
            file.write(body)
            file.flush()
            run("gh", "pr", "create", "--repo", repo, "--base", BASE, "--head", branch,
                "--title", f"Merge upstream {tag}", "--body-file", file.name)
        pr = api(f"repos/{repo}/pulls?state=open&base={BASE}&head={repo.split('/')[0]}:{branch}")[0]["number"]
    output(changed="true", head=head, base=base, pr=pr)
    print(f"Prepared https://github.com/{repo}/pull/{pr} at {head}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Command failed: {error.cmd}\n{error.stderr}") from error
