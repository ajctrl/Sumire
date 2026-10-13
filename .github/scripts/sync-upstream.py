"""Merge the latest stable upstream release, preserving Git ancestry and fork changes."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


UPSTREAM = "KazumaProject/JapaneseKeyboard"
BASE = "dev"
STATE = Path(".github/upstream.json")


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
    # Reject rewritten/divergent release history instead of attempting a reset.
    git("merge-base", "--is-ancestor", state["commit"], upstream)
    git("merge-base", "--is-ancestor", state["commit"], base)
    branch = f"upstream/{tag}-{base[:12]}"
    git("switch", "-c", branch)
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    merged = run("git", "merge", "--no-ff", "--no-commit", upstream, check=False)
    if merged.returncode:
        conflicts = git("diff", "--name-only", "--diff-filter=U")
        git("merge", "--abort")
        raise RuntimeError(f"Manual upstream merge required:\n{conflicts}\n{merged.stderr}")
    # Never automatically change the code that grants approval to an update.
    protected = [".github/workflows/sync-upstream.yml",
                 ".github/workflows/android.yml",
                 ".github/workflows/fork-validation.yml",
                 ".github/scripts/release-upstream.py",
                 ".github/scripts/verify-release-apks.py",
                 ".github/scripts/sync-upstream.py",
                 ".github/scripts/check-fork-invariants.py",
                 ".github/scripts/tests", str(STATE)]
    if git("diff", "HEAD", "--", *protected):
        git("merge", "--abort")
        raise RuntimeError("Update changes fork automation; manual review required")
    run("python3", ".github/scripts/check-fork-invariants.py")
    STATE.write_text(json.dumps({"repository": UPSTREAM, "tag": tag,
                                "commit": upstream}, indent=2) + "\n")
    git("add", str(STATE))
    git("commit", "-m", f"Merge upstream {tag}")
    head = git("rev-parse", "HEAD")
    existing = git("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if existing:
        # A failed validation can be retried without force-pushing or duplicating PRs.
        git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
        previous = git("rev-parse", "FETCH_HEAD")
        if (git("rev-parse", f"{previous}^{{tree}}") != git("rev-parse", "HEAD^{tree}")
                or git("show", "-s", "--format=%P", previous) != f"{base} {upstream}"):
            raise RuntimeError("Existing update branch was modified; manual review required")
        head = previous
    else:
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
