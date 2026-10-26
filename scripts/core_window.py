# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///

"""Resolve the two released Core generations; never infer a release from a date."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

INITIAL_MINIMUM = "2026.10.0"
STABLE_URL = "https://version.home-assistant.io/stable.json"
RELEASES_URL = "https://pypi.org/pypi/homeassistant/json"
PYTHON_URL = (
    "https://raw.githubusercontent.com/home-assistant/core/{version}/.python-version"
)
FLOORS = ("MIN_EMBEDDED_HOME_ASSISTANT_VERSION", "MIN_NATIVE_HOME_ASSISTANT_VERSION")
ARCHITECTURES = {"amd64": "ubuntu-24.04", "aarch64": "ubuntu-24.04-arm"}
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}\.[1-9][0-9]?\.(?:0|[1-9][0-9]*)", value
    ):
        raise ValueError(f"Expected a stable Core release, got {value!r}")
    year, month, patch = map(int, value.split("."))
    if not 1 <= month <= 12:
        raise ValueError(f"Invalid Core release month: {value}")
    return year, month, patch


def minimum_for(latest: str) -> str:
    year, month, _ = version_tuple(latest)
    previous = (year - 1, 12, 0) if month == 1 else (year, month - 1, 0)
    if version_tuple(latest) < version_tuple(INITIAL_MINIMUM):
        raise ValueError("Stable Core predates the initial component minimum")
    return ".".join(map(str, max(previous, version_tuple(INITIAL_MINIMUM))))


def select_versions(latest: str, releases: Iterable[str]) -> list[str]:
    """Keep the exact floor and latest published patches of the two trains."""
    minimum = minimum_for(latest)
    lower, upper = version_tuple(minimum), version_tuple(latest)
    stable = set()
    for release in releases:
        try:
            parsed = version_tuple(release)
        except ValueError:
            continue
        if lower <= parsed <= upper:
            stable.add(parsed)
    if lower not in stable or upper not in stable:
        raise ValueError(
            "The stable channel or exact minimum is missing from published releases"
        )
    months = {lower[:2], upper[:2]}
    chosen = {lower}
    for month in months:
        patches = [release for release in stable if release[:2] == month]
        if not patches:
            raise ValueError(f"No stable release for required generation {month}")
        chosen.add(max(patches))
    return [".".join(map(str, release)) for release in sorted(chosen)]


def fetch_text(url: str) -> str:
    """Bound retries, socket time and response size; outages never freeze a pin."""
    request = Request(url, headers={"User-Agent": "ha-mcp-core-window/1"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=15) as response:
                data: bytes = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError(f"Core metadata response exceeds size limit: {url}")
            return data.decode("utf-8")
        except HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise RuntimeError(f"Core metadata HTTP {error.code}: {url}") from error
            last_error: Exception = error
        except (URLError, TimeoutError, OSError) as error:
            last_error = error
        if attempt < 2:
            time.sleep(attempt + 1)
    raise RuntimeError(
        f"Core metadata unavailable after 3 attempts: {url}"
    ) from last_error


def make_window(latest: str, versions: dict[str, str]) -> dict[str, Any]:
    selected = select_versions(latest, versions)
    if set(selected) != set(versions):
        raise ValueError(
            "Window must contain only the exact minimum and latest patches"
        )
    for python in versions.values():
        if not re.fullmatch(r"3\.[0-9]+\.[0-9]+", python):
            raise ValueError(f"Invalid Core Python version: {python!r}")
    return {
        "schema": 1,
        "latest": latest,
        "minimum": minimum_for(latest),
        "generations": sorted(
            {".".join(v.split(".")[:2]) for v in selected},
            key=lambda v: tuple(map(int, v.split("."))),
        ),
        "versions": versions,
        "matrix": {
            "include": [
                {"core": core, "python": versions[core], "arch": arch, "os": runner}
                for core in selected
                for arch, runner in ARCHITECTURES.items()
            ]
        },
    }


def resolve_window(fetch: Callable[[str], str] = fetch_text) -> dict[str, Any]:
    channel = json.loads(fetch(STABLE_URL))
    if channel.get("channel") != "stable":
        raise ValueError("Core channel metadata does not identify the stable channel")
    latest = channel["homeassistant"]["default"]
    version_tuple(latest)
    index = json.loads(fetch(RELEASES_URL))
    releases = [
        version
        for version, files in index["releases"].items()
        if any(not file.get("yanked", False) for file in files)
    ]
    selected = select_versions(latest, releases)
    versions = {
        version: fetch(PYTHON_URL.format(version=version)).strip()
        for version in selected
    }
    return {
        **make_window(latest, versions),
        "resolved_at": datetime.now(UTC).isoformat(),
        "sources": {
            "stable": STABLE_URL,
            "releases": RELEASES_URL,
            "python": PYTHON_URL,
        },
    }


def load_window(path: Path) -> dict[str, Any]:
    window: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    canonical = make_window(window["latest"], window["versions"])
    for key, expected in canonical.items():
        if window.get(key) != expected:
            raise ValueError(f"Inconsistent Core window field: {key}")
    return window


def check_floor(
    repository: Path, window: dict[str, Any], *, revision: str | None = None
) -> None:
    """Reject a new source release advertising an unqualified Core minimum."""

    def read(relative: str) -> str:
        if revision is None:
            return (repository / relative).read_text(encoding="utf-8")
        import subprocess

        return subprocess.run(
            ["git", "show", f"{revision}:{relative}"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    component = "custom_components/ha_mcp_tools/"
    declarations = {"hacs.json": json.loads(read("hacs.json"))["homeassistant"]}
    constants = ast.parse(read(component + "const.py"))
    for name in FLOORS:
        values = [
            ast.literal_eval(node.value)
            for node in constants.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            )
        ]
        declarations[name] = values[0] if len(values) == 1 else None
    declarations[component + "hacs.json"] = json.loads(read(component + "hacs.json"))[
        "homeassistant"
    ]
    drift = {
        name: value
        for name, value in declarations.items()
        if value != window["minimum"]
    }
    if drift:
        raise ValueError(
            f"Core window requires minimum {window['minimum']}; raise the HACS and runtime floors together before release: {drift}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--from-file", type=Path, help="Use this run's already-resolved matrix"
    )
    parser.add_argument("--check-floor", action="store_true")
    parser.add_argument(
        "--repository", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--revision", help="Check an immutable release tree, not the current checkout"
    )
    options = parser.parse_args()
    window = load_window(options.from_file) if options.from_file else resolve_window()
    rendered = json.dumps(window, indent=2) + "\n"
    if options.output:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write("matrix=" + json.dumps(window["matrix"]) + "\n")
            stream.write(f"latest={window['latest']}\nminimum={window['minimum']}\n")
            stream.write(f"python={window['versions'][window['latest']]}\n")
    if options.check_floor:
        check_floor(options.repository, window, revision=options.revision)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        print(f"Core window resolution failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
