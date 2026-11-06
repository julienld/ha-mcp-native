# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///

"""Run one disposable Docker bench. No HA URL, token, host mount or publish option."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def run(image: str, output: Path, topology: str) -> dict[str, Any]:
    if shutil.which("docker") is None:
        raise RuntimeError(
            "Docker is unavailable. No real Core/HACS qualification was executed."
        )
    platform_info = json.loads(docker("info", "--format", "{{json .}}").stdout)
    if platform_info["OSType"] != "linux":
        raise RuntimeError(
            "This bench requires a Linux Docker engine; no WSL launcher is provided."
        )
    if output.exists():
        raise FileExistsError(
            f"Use a fresh evidence directory, refusing stale results: {output}"
        )
    output.mkdir(parents=True)
    container = "ha-mcp-hacs-qualification-" + uuid.uuid4().hex[:10]
    started = False
    try:
        docker("run", "-d", "--network", "none", "--name", container, image)
        started = True
        inspect = json.loads(docker("inspect", container).stdout)[0]
        assert inspect["HostConfig"]["NetworkMode"] == "none"
        assert not inspect["HostConfig"].get("Binds")
        assert not inspect["HostConfig"].get("PortBindings")
        (output / "isolation.json").write_text(
            json.dumps(
                {
                    "network_mode": "none",
                    "host_binds": [],
                    "published_ports": [],
                    "image_id": inspect["Image"],
                    "topology": topology,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        for phase in ("exercise", "after-restart"):
            if phase == "after-restart":
                docker("restart", "--time", "30", container)
            result = docker(
                "exec",
                container,
                "python",
                "/opt/qualification/driver.py",
                phase,
                "--topology",
                topology,
                check=False,
            )
            (output / f"{phase}.log").write_text(
                result.stdout + result.stderr, encoding="utf-8"
            )
            print(result.stdout, end="", flush=True)
            if result.returncode:
                raise RuntimeError(
                    f"Real owner qualification failed in {phase}; see {output / (phase + '.log')}"
                )
    finally:
        if started:
            copied = docker(
                "cp",
                f"{container}:/qualification-evidence/evidence.json",
                str(output / "evidence.json"),
                check=False,
            )
            logs = docker("logs", container, check=False)
            (output / "ha.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
            docker("rm", "-f", container)
            if copied.returncode:
                (output / "evidence-unavailable.txt").write_text(
                    "The driver did not produce evidence. This run did not qualify HACS.\n"
                )
    evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["complete"] is True, evidence
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="ha-mcp-hacs-qualification")
    parser.add_argument(
        "--output", type=Path, default=Path("tests/native/qualification_hacs/out")
    )
    parser.add_argument(
        "--topology", choices=("server-only", "both"), default="server-only"
    )
    options = parser.parse_args()
    print(json.dumps(run(options.image, options.output, options.topology), indent=2))
