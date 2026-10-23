# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///

"""Record identical behavioral tests and immutable application source for each run."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def git(directory: Path, *arguments: str) -> str:
    return subprocess.check_output(
        ["git", *arguments], cwd=directory, text=True
    ).strip()


def record(source: Path, harness: Path, role: str, expected: str, output: Path) -> None:
    revision = git(source, "rev-parse", "HEAD")
    if revision != expected:
        raise ValueError(
            "Source checkout does not match the requested immutable revision"
        )
    protected = ("src", "custom_components", "pyproject.toml", "uv.lock")
    if git(source, "status", "--porcelain", "--untracked-files=all", "--", *protected):
        raise ValueError("Application source was modified by the continuity harness")
    relative = Path("tests/src/e2e/continuity")
    hashes = {
        path.relative_to(source / relative).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted((source / relative).glob("*.py"))
    }
    originals = {
        path.relative_to(harness / relative).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted((harness / relative).glob("*.py"))
    }
    if not hashes or hashes != originals:
        raise ValueError(
            "Before and after must execute the exact same behavioral tests"
        )
    result = {
        "role": role,
        "source_revision": revision,
        "harness_revision": git(harness, "rev-parse", "HEAD"),
        "application_source_unchanged": True,
        "test_files_sha256": hashes,
        "qualification_fixtures_sha256": {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in (
                "tests/src/e2e/_conftest_embedded.py",
                "tests/src/e2e/_conftest_testcontainer.py",
                "tests/src/e2e/_conftest_native_runtime.py",
                "tests/qualification_artifacts.py",
            )
        },
        "source_trees": {
            name: git(source, "rev-parse", f"HEAD:{name}") for name in protected
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--role", choices=("before", "after"), required=True)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    record(
        args.source.resolve(),
        args.harness.resolve(),
        args.role,
        args.expected,
        args.output,
    )


if __name__ == "__main__":
    main()
