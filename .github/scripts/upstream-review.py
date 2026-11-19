"""Create one draft review handoff per base/upstream pair, even without Issues."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def marker(base, upstream):
    key = hashlib.sha256(f"{base}:{upstream}".encode()).hexdigest()
    return f"<!-- sumire-upstream-review:{key} -->"


def api(path, payload=None):
    command = ["gh", "api", path]
    if payload is not None:
        command += ["--method", "POST", "--input", "-"]
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def find(repo, base, upstream):
    identity = marker(base, upstream)
    page = 1
    while True:
        pulls = api(f"repos/{repo}/pulls?state=all&base=dev&per_page=100&page={page}")
        for pull in pulls:
            if (identity in (pull.get("body") or "")
                    and (pull.get("user") or {}).get("login") == "github-actions[bot]"):
                return pull
        if len(pulls) < 100:
            return None
        page += 1


def report_branch(base, upstream, tag, body):
    """Commit only a review report using a separate index; never commit conflicts."""
    branch = f"upstream-review/{tag}-{base[:12]}-{upstream[:12]}"
    def git(*args, input=None, env=None):
        return subprocess.run(["git", *args], input=input, env=env, text=True,
                              capture_output=True, check=True).stdout.strip()
    if git("ls-remote", "--heads", "origin", f"refs/heads/{branch}"):
        return branch  # Recover a push that succeeded before PR creation failed.
    with tempfile.TemporaryDirectory() as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / "index"),
                   GIT_AUTHOR_NAME="github-actions[bot]", GIT_COMMITTER_NAME="github-actions[bot]",
                   GIT_AUTHOR_EMAIL="41898282+github-actions[bot]@users.noreply.github.com",
                   GIT_COMMITTER_EMAIL="41898282+github-actions[bot]@users.noreply.github.com")
        git("read-tree", base, env=env)
        blob = git("hash-object", "-w", "--stdin", input=body, env=env)
        git("update-index", "--add", "--cacheinfo", "100644", blob,
            f"docs/upstream-reviews/{base[:12]}-{upstream[:12]}.md", env=env)
        commit = git("commit-tree", git("write-tree", env=env), "-p", base,
                     "-m", f"Record upstream {tag} human review request", env=env)
        git("push", "origin", f"{commit}:refs/heads/{branch}")
    return branch


def record(repo, base, upstream, tag, reason, patch=""):
    existing = find(repo, base, upstream)
    if existing is None:
        url = f"https://github.com/{repo}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
        body = (f"{marker(base, upstream)}\n## Human review required\n\n{reason}\n\n"
                f"Base: `{base}`\nUpstream: `{upstream}` ({tag})\n\n"
                f"[Preparation run and review artifacts]({url})\n\n"
                "This draft contains a review report only, NOT the application update. "
                "Inspect the upstream-review artifact for an available candidate patch; "
                "it may contain unresolved conflicts and must not be applied blindly. "
                "Do not merge this report as a substitute for integrating the upstream update. "
                "Resolve the update in a separate branch, validate it, and update .github/upstream.json. "
                "The same revision pair remains blocked even if this draft is closed or marked ready.\n")
        branch = report_branch(base, upstream, tag, body)
        existing = api(f"repos/{repo}/pulls", {"title": f"Review upstream {tag} update ({base[:12]})",
                       "head": branch, "base": "dev", "draft": True, "body": body})
    evidence(existing, base, upstream, reason, patch)
    return existing


def evidence(pull, base, upstream, reason, patch=""):
    directory = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "upstream-review"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.md").write_text(
        f"# Human review required\n\n{reason}\n\n{pull['html_url']}\n\n"
        f"Base: {base}\nUpstream: {upstream}\n\n"
        "Candidate patch is inspection-only and may contain unresolved conflicts.\n", encoding="utf-8")
    if patch:
        (directory / "candidate.patch").write_text(patch, encoding="utf-8")
