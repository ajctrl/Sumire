"""Structural checks; semantic risk decisions belong to the model and validation."""

from pathlib import PurePosixPath
import re


VERSION = "sol-high-risk-review-v1"
HUNK = re.compile(r"^<<<<<<< [^\n]*\n(.*?)^\|\|\|\|\|\|\| [^\n]*\n(.*?)"
                  r"^=======\n(.*?)^>>>>>>> [^\n]*(?:\n|$)", re.M | re.S)
MARKER = re.compile(r"^(?:<{7}|\|{7}|={7}|>{7})(?: |$)", re.M)


class ReviewRequired(RuntimeError):
    pass


def stop(path, reason):
    raise ReviewRequired(f"Manual review required: {path}: {reason}")


def kind_for_path(path):
    file = PurePosixPath(path)
    if (not path or file.is_absolute() or ".." in file.parts or "\\" in path
            or str(file) != path or any(part in (".git", ".github") for part in file.parts)):
        stop(path, "automation or non-canonical path")
    return "text"


def check_conflict(path, content):
    kind = kind_for_path(path)
    if len(content.encode()) > 8_000_000 or "\0" in content:
        stop(path, "binary or oversized content")
    hunks = list(HUNK.finditer(content))
    if not hunks or len(re.findall(r"^<<<<<<< ", content, re.M)) != len(hunks):
        stop(path, "requires ordinary diff3 conflict hunks")
    # Reject nested/malformed markers, including stray markers outside a hunk.
    for hunk in hunks:
        if any(MARKER.search(side) for side in hunk.groups()):
            stop(path, "unsupported or nested conflict markers")
    if MARKER.search(HUNK.sub("", content)):
        stop(path, "unsupported conflict markers outside hunks")
    return kind


def validate_replacements(path, content, replacements):
    check_conflict(path, content)
    if (not isinstance(replacements, list) or len(replacements) != len(list(HUNK.finditer(content)))
            or any(not isinstance(value, str) or "\0" in value or MARKER.search(value)
                   for value in replacements)):
        stop(path, "invalid or unresolved replacements")
