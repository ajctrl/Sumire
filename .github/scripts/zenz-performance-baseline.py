"""Compare a release sync against that release, keeping benchmark assets equal."""
import json
import re
import subprocess
import sys


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def select_baseline(base):
    base_sha = git("rev-parse", "--verify", f"{base}^{{commit}}")
    previous = json.loads(git("show", f"{base_sha}:.github/upstream.json"))
    current = json.loads(git("show", "HEAD:.github/upstream.json"))
    if previous["commit"] == current["commit"]:
        return base_sha

    # An upstream release changes the dictionaries as well as the converter.
    # Benchmark the fork against the exact imported release, not the old release.
    if current["repository"] != "KazumaProject/JapaneseKeyboard":
        raise ValueError("Unexpected upstream repository")
    commit = current["commit"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid upstream commit")
    subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"], check=True)
    return commit


if __name__ == "__main__":
    print(f"ZENZ_PERF_BASELINE_REF={select_baseline(sys.argv[1])}")
