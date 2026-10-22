"""Run with python -m tests.native.minimal_qualification inside a Linux Core image.

This installs only the Core distribution in a fresh isolated venv. It does not
inherit the image's requirements_all environment or claim public-URL proof.
"""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import venv
from pathlib import Path
from typing import Any

from .minimal_support import sanitize, save

CORE_VERSION = "2026.10.2"
CORE_UV_VERSION = "0.12.23"
CERTIFI_BASELINE = "2026.7.22"
ROOT = Path(__file__).resolve().parents[2]


def isolated_environment(directory: Path) -> dict[str, str]:
    result = dict(os.environ)
    for name in (
        "PYTHONHOME",
        "UV_PYTHON",
        "UV_SYSTEM_PYTHON",
        "UV_PROJECT_ENVIRONMENT",
    ):
        result.pop(name, None)
    result.update(
        VIRTUAL_ENV=str(directory),
        PYTHONNOUSERSITE="1",
        PYTHONPATH=str(ROOT),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return result


def run_logged(command: list[str], *, env: dict[str, str], log: Path) -> None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=1200,
        check=False,
    )
    with log.open("a", encoding="utf-8") as stream:
        stream.write(sanitize(result.stdout + result.stderr) + "\n")
    if result.returncode:
        raise RuntimeError(f"Minimal qualification command failed; see {log.name}")


def qualify(assets: Path, work: Path, output: Path) -> None:
    if sys.platform != "linux" or sys.version_info < (3, 14, 2):
        raise RuntimeError(
            "The real minimal Core qualification needs Linux Python 3.14.2+"
        )
    core = importlib.util.find_spec("homeassistant")
    if core is None or core.origin is None:
        raise RuntimeError(
            "Run inside the pinned Core image to supply its bootstrap constraints"
        )
    constraints = Path(core.origin).parent / "package_constraints.txt"
    if not constraints.is_file():
        raise RuntimeError("The Core image has no package_constraints.txt")
    work.mkdir(parents=True, exist_ok=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    log = output.with_suffix(".log")
    log.write_text("", encoding="utf-8")
    directory = work / "venv"
    proof: dict[str, Any] = {
        "mode": "minimal_Core_local_paired_artifacts",
        "complete": False,
        "core_version": CORE_VERSION,
        "bootstrap_constraints": str(constraints),
        "old_certifi_seed": CERTIFI_BASELINE,
    }
    save(output, proof)
    try:
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(directory)
        python = directory / "bin/python"
        env = isolated_environment(directory)
        run_logged(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--constraint",
                str(constraints),
                f"homeassistant=={CORE_VERSION}",
                f"uv=={CORE_UV_VERSION}",
            ],
            env=env,
            log=log,
        )
        run_logged(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--constraint",
                str(constraints),
                f"certifi=={CERTIFI_BASELINE}",
            ],
            env=env,
            log=log,
        )
        run_logged(
            [
                str(python),
                "-m",
                "tests.native.minimal_install",
                "--assets",
                str(assets.resolve()),
                "--work-directory",
                str(work.resolve()),
                "--output",
                str(output.resolve()),
            ],
            env=env,
            log=log,
        )
    except BaseException as error:
        recorded = json.loads(output.read_text(encoding="utf-8"))
        recorded["complete"] = False
        recorded.setdefault(
            "failure", {"type": type(error).__name__, "message": sanitize(str(error))}
        )
        save(output, recorded)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    qualify(args.assets, args.work_directory, args.output)


if __name__ == "__main__":
    main()
