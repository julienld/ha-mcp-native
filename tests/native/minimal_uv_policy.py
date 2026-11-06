"""Ordinary uv cooldown policy for the disposable Core installer."""

import tomllib
from datetime import datetime
from pathlib import Path
from typing import Any


def native_install_environment(
    environment: dict[str, str],
    work: Path,
    policy: Path,
    *,
    now: datetime,
) -> tuple[dict[str, str], dict[str, Any]]:
    settings = tomllib.loads(policy.read_text(encoding="utf-8"))["tool"]["uv"]
    result = dict(environment)
    result.setdefault("UV_EXCLUDE_NEWER", settings["exclude-newer"])
    if settings.get("exclude-newer-package"):
        raise ValueError("Per-package uv cooldown exceptions are not supported")
    proof: dict[str, Any] = {
        "global_exclude_newer": result["UV_EXCLUDE_NEWER"],
        "exception_active": False,
        "scope": "native_install_Core_uv_only",
    }
    return result, proof
