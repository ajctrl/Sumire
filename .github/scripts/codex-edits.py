"""Validate and transfer the files edited in an isolated Codex workspace."""

import hashlib
from pathlib import Path
import stat

IGNORED = {".git", ".codex", ".agents", "__pycache__"}
PROTECTED = {".github", ".git", ".codex", ".agents"}
MAX_EDITS_BYTES = 8_000_000


def inventory(root):
    result = {}
    def visit(directory):
        for file in directory.iterdir():
            if file.name in IGNORED:
                continue
            relative = file.relative_to(root).as_posix()
            mode = file.lstat().st_mode
            if stat.S_ISLNK(mode):
                result[relative] = ("symlink", file.readlink().as_posix())
            elif stat.S_ISDIR(mode):
                visit(file)
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256()
                binary = False
                with file.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(65536), b""):
                        digest.update(chunk)
                        binary = binary or b"\0" in chunk
                result[relative] = ("file", digest.hexdigest(), bool(mode & 0o111), binary)
            else:
                raise RuntimeError(f"Unsupported workspace entry: {relative}")
    visit(Path(root))
    return result


def safe_path(root, path):
    relative = Path(path)
    if (not path or relative.is_absolute() or str(relative) != path or ".." in relative.parts
            or "\\" in path or any(part in PROTECTED for part in relative.parts)):
        raise RuntimeError(f"Codex changed a protected or invalid path: {path}")
    target = Path(root)
    for part in relative.parts:
        target /= part
        if target.is_symlink():
            raise RuntimeError(f"Codex changed a symlink: {path}")
    return target


def changes(root, before):
    after = inventory(root)
    edits = {}
    size = 0
    for path in sorted(before.keys() | after.keys()):
        if before.get(path) == after.get(path):
            continue
        safe_path(root, path)
        if any(value and value[0] != "file" for value in (before.get(path), after.get(path))):
            raise RuntimeError(f"Codex changed a symlink: {path}")
        if any(value and value[0] == "file" and value[3] for value in (before.get(path), after.get(path))):
            raise RuntimeError(f"Codex changed a binary file: {path}")
        if path not in after:
            edits[path] = None
            continue
        file = Path(root) / path
        size += file.stat().st_size
        if size > MAX_EDITS_BYTES:
            raise RuntimeError("Codex edits exceed checkpoint size; human review required")
        text = file.read_text(encoding="utf-8")
        if "\0" in text:
            raise RuntimeError(f"Codex changed a binary file: {path}")
        edits[path] = {"text": text, "executable": after[path][2]}
    return edits


def apply(root, edits):
    # Validate the entire set before applying any file, including restored data.
    targets = {path: safe_path(root, path) for path in edits}
    total = 0
    for path, value in edits.items():
        if targets[path].exists() and not targets[path].is_file():
            raise RuntimeError(f"Unsupported edit target: {path}")
        if value is not None:
            if (not isinstance(value, dict) or set(value) != {"text", "executable"}
                    or not isinstance(value["text"], str) or "\0" in value["text"]
                    or type(value["executable"]) is not bool):
                raise RuntimeError(f"Invalid saved edit: {path}")
            total += len(value["text"].encode())
    if total > MAX_EDITS_BYTES:
        raise RuntimeError("Codex edits exceed checkpoint size")
    for path, value in edits.items():
        file = targets[path]
        if value is None:
            file.unlink(missing_ok=True)
        else:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(value["text"], encoding="utf-8")
            file.chmod(0o755 if value["executable"] else 0o644)
