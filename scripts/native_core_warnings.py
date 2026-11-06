# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///

"""Reject Core warnings that identify HA-MCP's own deprecated or blocking calls."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

_RECORD = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OWNER = re.compile(
    r"(?:custom integration|integration) ['\"]ha_mcp_tools['\"]"
    r"|custom_components[/\\.]ha_mcp_tools(?:[/\\.]|\b)"
)
_CORE_LOGGER = re.compile(r"\[homeassistant(?:\.[\w.]+)?\]")
_DEPRECATED = re.compile(
    r"deprecated|deprecation|will (?:be removed|stop working)", re.I
)


def findings(log: str) -> list[dict[str, int | str]]:
    """Keep full records for attribution without copying arguments or secrets."""
    records: list[tuple[int, str]] = []
    start = 1
    lines: list[str] = []
    for number, line in enumerate(_ANSI.sub("", log).splitlines(), 1):
        if _RECORD.match(line) and lines:
            records.append((start, "\n".join(lines)))
            start, lines = number, []
        lines.append(line)
    if lines:
        records.append((start, "\n".join(lines)))
    result: list[dict[str, int | str]] = []
    for number, record in records:
        header = record.splitlines()[0]
        if not _CORE_LOGGER.search(header) or not _OWNER.search(record):
            continue
        if "Detected blocking call" in header:
            category = "blocking_call"
        elif _DEPRECATED.search(record):
            category = "deprecated_core_api"
        else:
            continue
        # The original log remains a CI artifact; a public summary never needs
        # its call arguments, credential-bearing URLs or arbitrary exception data.
        result.append({"line": number, "category": category})
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {
        "log": args.log.name,
        "findings": findings(args.log.read_text(encoding="utf-8")),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    if result["findings"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
