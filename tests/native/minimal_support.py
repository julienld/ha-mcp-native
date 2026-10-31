"""Environment evidence and log redaction shared by the minimal Core helpers."""

import json
import re
import runpy
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def sanitize(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        url = urlsplit(match[0])
        host = url.netloc.rsplit("@", 1)[-1]
        return urlunsplit((url.scheme, host, url.path, "", ""))

    return re.sub(r"https?://[^\s<>\"']+", replace, value)


@cache
def _receipt_helpers() -> dict[str, Any]:
    return runpy.run_path(
        str(Path(__file__).parent / "fixtures/native_fixture/dependency_receipt.py")
    )


def environment() -> dict[str, Any]:
    helpers = _receipt_helpers()
    return {
        "versions": helpers["installed_versions"](),
        "issues": helpers["dependency_issues"](),
    }


def changes(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    constraint_path: Path | None = None,
    previous_versions: dict[str, str] | None = None,
    baseline_constraints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _receipt_helpers()["assess_dependencies"](
        before["versions"],
        after["versions"],
        before["issues"],
        after["issues"],
        constraint_path=constraint_path,
        previous_versions=previous_versions,
        baseline_constraints=baseline_constraints,
    )


def core_constraint_context(path: Path) -> dict[str, Any]:
    context = _receipt_helpers()["core_constraint_context"](path)
    return {key: value for key, value in context.items() if key != "text"}


def save(path: Path, proof: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
