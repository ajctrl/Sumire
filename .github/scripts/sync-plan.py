"""Pass a validated update plan between preparation and validation workflows."""

import json
import os
from pathlib import Path
import re
import sys


FIELDS = {"version", "repository", "run_id", "run_attempt", "changed", "paused", "base", "head", "pr"}


def validate(plan, repository, run_id, run_attempt):
    if (not isinstance(plan, dict) or set(plan) != FIELDS
            or type(plan["version"]) is not int or plan["version"] != 1
            or plan["repository"] != repository
            or type(plan["run_id"]) is not int or plan["run_id"] != int(run_id) or plan["run_id"] <= 0
            or type(plan["run_attempt"]) is not int or plan["run_attempt"] != int(run_attempt)
            or plan["run_attempt"] <= 0
            or type(plan["changed"]) is not bool or type(plan["paused"]) is not bool
            or (plan["changed"] and plan["paused"])
            or not isinstance(plan["base"], str) or not re.fullmatch(r"[0-9a-f]{40}", plan["base"])):
        raise ValueError("Invalid prepared update or workflow provenance")
    if plan["changed"]:
        if (not isinstance(plan["head"], str) or not re.fullmatch(r"[0-9a-f]{40}", plan["head"])
                or type(plan["pr"]) is not int or plan["pr"] <= 0):
            raise ValueError("Invalid prepared update head or PR")
    elif plan["head"] != "" or plan["pr"] != "":
        raise ValueError("An unchanged or paused plan cannot carry an update")
    return plan


def boolean(value):
    if value not in ("true", "false"):
        raise ValueError("Missing or invalid preparation outcome")
    return value == "true"


def write(path):
    plan = {"version": 1, "repository": os.environ["GITHUB_REPOSITORY"],
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "changed": boolean(os.environ["SYNC_CHANGED"]),
            "paused": boolean(os.environ["SYNC_PAUSED"]),
            "base": os.environ["SYNC_BASE"], "head": os.environ["SYNC_HEAD"],
            "pr": int(os.environ["SYNC_PR"]) if os.environ["SYNC_PR"] else ""}
    validate(plan, plan["repository"], plan["run_id"], plan["run_attempt"])
    path.write_text(json.dumps(plan) + "\n")


def read(path):
    plan = validate(json.loads(path.read_text()), os.environ["GITHUB_REPOSITORY"],
                    os.environ["EXPECTED_RUN_ID"], os.environ["EXPECTED_RUN_ATTEMPT"])
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        for field in ("changed", "paused", "base", "head", "pr"):
            value = str(plan[field]).lower() if type(plan[field]) is bool else str(plan[field])
            output.write(f"{field}={value}\n")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("write", "read"):
        raise SystemExit("Usage: sync-plan.py write|read PATH")
    (write if sys.argv[1] == "write" else read)(Path(sys.argv[2]))
