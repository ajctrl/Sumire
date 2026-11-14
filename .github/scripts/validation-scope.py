"""Select Full validation when changes affect its build/native/dictionary paths."""

from pathlib import PurePosixPath
import sys
import re
import subprocess


def requires_full(paths):
    for path in paths:
        file = PurePosixPath(path)
        parts = file.parts
        lower = path.lower()
        if (file.suffix in (".gradle", ".kts", ".cmake", ".cpp", ".cc", ".c", ".h", ".hpp", ".so")
                or file.name in ("CMakeLists.txt", "gradle.properties", "libs.versions.toml")
                or any(part in ("gradle", "buildSrc", "build-logic", "jniLibs", "cpp") for part in parts)
                or path == ".gitmodules" or "src/full" in path
                or any(part in ("zenz", "gemma", "mozc") for part in parts)
                or ("/src/" in path and "/assets/" in path)
                or "dictionary" in lower or path.startswith("tools/system_ngram/")):
            return True
    return False


def requires_full_revision(base, head):
    paths = subprocess.check_output(["git", "diff", "--name-only", "-z", base, head], text=True).split("\0")
    if "app/build.gradle" in paths:
        diff = subprocess.check_output(["git", "diff", "--no-ext-diff", base, head, "--", "app/build.gradle"], text=True)
        lines = [line[1:] for line in diff.splitlines()
                 if line.startswith(("+", "-")) and not line.startswith(("+++", "---")) and line[1:].strip()]
        if lines and all(re.fullmatch(r'\s*version(?:Name "[^"\n]+"|Code [0-9]+)\s*', line) for line in lines):
            paths.remove("app/build.gradle")
    return requires_full(paths)


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "--base" and sys.argv[3] == "--head":
        full = requires_full_revision(sys.argv[2], sys.argv[4])
    else:
        full = requires_full(sys.stdin.read().splitlines())
    print("full=" + str(full).lower())
