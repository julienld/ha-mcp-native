"""Run with python -m tests.native.minimal_qualification inside a Linux Core image.

This installs Core and its exact frontend in a fresh isolated venv. It does not
inherit the image's requirements_all environment or claim public-URL proof.
"""

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import subprocess
import sys
import tomllib
import venv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import url2pathname

from scripts.native_published_assets import REPOSITORY

from .minimal_support import sanitize, save
from .minimal_uv_policy import native_install_environment
from .runtime_attestation import attest_runtime

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
    # Core's uv subprocess inherits cwd. Keep it outside the source checkout
    # so its dependency installation does not discover our project policy.
    # PYTHONPATH still locates this harness; explicit user UV_* settings remain.
    directory = Path(env["VIRTUAL_ENV"]).resolve().parent
    if directory.is_relative_to(ROOT.resolve()):
        raise RuntimeError("Minimal Core work directory must be outside the checkout")
    result = subprocess.run(
        command,
        cwd=directory,
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


def core_frontend_requirement(core_directory: Path) -> str:
    """Keep the UI shipped with the selected Core in the Core bootstrap phase."""
    manifest = core_directory / "components/frontend/manifest.json"
    requirements = json.loads(manifest.read_bytes()).get("requirements")
    if (
        not isinstance(requirements, list)
        or len(requirements) != 1
        or not isinstance(requirements[0], str)
        or not re.fullmatch(r"home-assistant-frontend==\d+(?:\.\d+)+", requirements[0])
    ):
        raise ValueError("Core frontend must declare its single exact distribution")
    return requirements[0]


def qualify(
    assets: Path,
    work: Path,
    output: Path,
    *,
    image_receipt: Path,
    expected_core: str,
    expected_arch: str,
    python_source: str,
    repository: str = REPOSITORY,
) -> None:
    if sys.platform != "linux":
        raise RuntimeError("Run qualification with the Linux Core image's Python")
    output.parent.mkdir(parents=True, exist_ok=True)
    proof: dict[str, Any] = {
        "mode": "minimal_Core_local_paired_artifacts",
        "complete": False,
    }
    save(output, proof)
    proof["runtime_attestation"] = attest_runtime(
        image_receipt,
        expected_core=expected_core,
        expected_arch=expected_arch,
        python_source=python_source,
        requires_python=tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]["requires-python"],
    )
    save(output, proof)
    print(json.dumps(proof["runtime_attestation"], indent=2), flush=True)
    core = importlib.util.find_spec("homeassistant")
    if core is None or core.origin is None:
        raise RuntimeError(
            "Run inside the pinned Core image to supply its bootstrap constraints"
        )
    constraints = Path(core.origin).parent / "package_constraints.txt"
    if not constraints.is_file():
        raise RuntimeError("The Core image has no package_constraints.txt")
    distribution = importlib.metadata.distribution("homeassistant")
    expected = Path(distribution.locate_file("homeassistant/package_constraints.txt"))
    # Core Container installs its source tree editable; wheels own the file
    # directly. Bind the constraints to the distribution in both layouts.
    direct_url = json.loads(distribution.read_text("direct_url.json") or "{}")
    if direct_url.get("dir_info", {}).get("editable"):
        source = urlsplit(direct_url["url"])
        if source.scheme != "file" or source.netloc not in ("", "localhost"):
            raise RuntimeError("The installed Core has no local editable source")
        expected = (
            Path(url2pathname(source.path)) / "homeassistant/package_constraints.txt"
        )
    if constraints.resolve() != expected.resolve():
        raise RuntimeError("Bootstrap constraints do not belong to the installed Core")
    core_version = distribution.version
    uv_version = importlib.metadata.distribution("uv").version
    frontend_requirement = core_frontend_requirement(constraints.parent)
    work.mkdir(parents=True, exist_ok=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    log = output.with_suffix(".log")
    log.write_text("", encoding="utf-8")
    directory = work / "venv"
    proof.update(
        {
            "core_version": core_version,
            "bootstrap_frontend": frontend_requirement,
            "uv_version": uv_version,
            "interpreter": {
                "executable": sys.executable,
                "version": sys.version,
                "implementation": sys.implementation.name,
                "architecture": platform.machine(),
            },
            "bootstrap_constraints": str(constraints),
            "bootstrap_constraints_sha256": hashlib.sha256(
                constraints.read_bytes()
            ).hexdigest(),
            "old_certifi_seed": CERTIFI_BASELINE,
            "bootstrap_policy": "pip_Core_and_exact_Core_frontend_under_image_constraints_then_old_certifi_seed; uv_cutoff_applies_to_native_install",
        }
    )
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
                f"homeassistant=={core_version}",
                frontend_requirement,
                f"uv=={uv_version}",
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
        install_env, proof["native_install_policy"] = native_install_environment(
            env, work, ROOT / "pyproject.toml", now=datetime.now(UTC)
        )
        save(output, proof)
        print(json.dumps(proof["native_install_policy"], indent=2), flush=True)
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
                "--repository",
                repository,
            ],
            env=install_env,
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
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--image-receipt", type=Path, required=True)
    parser.add_argument("--expected-core", required=True)
    parser.add_argument("--expected-arch", required=True)
    parser.add_argument("--python-source", required=True)
    args = parser.parse_args()
    qualify(
        args.assets,
        args.work_directory,
        args.output,
        repository=args.repository,
        image_receipt=args.image_receipt,
        expected_core=args.expected_core,
        expected_arch=args.expected_arch,
        python_source=args.python_source,
    )


if __name__ == "__main__":
    main()
