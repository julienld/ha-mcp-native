"""Temporary security policy for the disposable Core installer, never production."""

import hashlib
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2
# Remove after this upload has passed the ordinary seven-day cooldown.
EXPIRES = datetime(2026, 10, 27, 2, 24, 17, tzinfo=UTC)
CERTIFI_CUTOFF = "2026-10-21T00:00:00Z"


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
    exception = settings.get("exclude-newer-package", {})
    if exception and exception != {"certifi": CERTIFI_CUTOFF}:
        raise ValueError("Only the reviewed, date-bounded certifi exception is allowed")
    proof: dict[str, Any] = {
        "global_exclude_newer": result["UV_EXCLUDE_NEWER"],
        "exception_active": bool(exception) and now < EXPIRES,
        "exception_expires": EXPIRES.isoformat(),
        "scope": "native_install_Core_uv_only",
    }
    if proof["exception_active"]:
        if result.get("UV_CONFIG_FILE") or result.get(
            "UV_NO_CONFIG", ""
        ).lower() not in ("", "0", "false"):
            raise ValueError("Cannot replace explicit operator uv configuration")
        config = work / "native-install-uv.toml"
        config.write_text(
            '[pip]\nexclude-newer-package = { certifi = "' + CERTIFI_CUTOFF + '" }\n',
            encoding="utf-8",
        )
        result["UV_CONFIG_FILE"] = str(config.resolve())
        proof.update(
            exclude_newer_package=exception,
            config_sha256=hashlib.sha256(config.read_bytes()).hexdigest(),
        )
    return result, proof
