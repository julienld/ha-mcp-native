"""Worker inside the isolated venv: use Core's real installation decisions."""

import argparse
import asyncio
import importlib
import importlib.metadata
import json
import site
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from subprocess import SubprocessError
from typing import Any

from scripts.native_published_assets import (
    release_contract,
    verify_component,
    verify_wheel,
)

from .minimal_support import changes, environment, sanitize, save


def assert_isolated() -> None:
    prefix = Path(sys.prefix).resolve()
    if sys.prefix == sys.base_prefix or site.ENABLE_USER_SITE:
        raise RuntimeError(
            "The minimal worker is not isolated from system/user packages"
        )
    outside = [
        distribution.metadata["Name"]
        for distribution in importlib.metadata.distributions()
        if not Path(str(distribution.locate_file(""))).resolve().is_relative_to(prefix)
    ]
    if outside:
        raise RuntimeError(
            f"Distributions escaped the isolated venv: {sorted(outside)}"
        )


def install_requirement(requirement: str, kwargs: dict[str, Any]) -> str:
    from homeassistant.requirements import _install_requirements_if_missing
    from homeassistant.util import package

    already = package.is_installed(requirement)
    installed, failed = _install_requirements_if_missing([requirement], kwargs)
    if failed or requirement not in installed:
        raise RuntimeError("Core refused a manifest requirement")
    return "already_satisfied" if already else "installed_by_Core"


def paired_requirement(
    requirement: str, dependency: str, wheel: Path
) -> tuple[str, bool]:
    """Only the verified candidate's own unpublished URL maps to its local wheel."""
    return (
        (f"ha-mcp @ {wheel.resolve().as_uri()}", True)
        if requirement == dependency
        else (requirement, False)
    )


def prepare_component(
    assets: Path, component: Path
) -> tuple[dict[str, Any], dict[str, str], Path, str]:
    build = json.loads((assets / "native-build.json").read_bytes())
    contract = release_contract(build, build["tag"], build["revision"])
    wheel = assets / contract["wheel"]
    verify_wheel(wheel, build, contract)
    manifest_hash = verify_component(assets / "ha_mcp_tools.zip", build, contract)
    component.mkdir(parents=True, exist_ok=False)
    with zipfile.ZipFile(assets / "ha_mcp_tools.zip") as archive:
        archive.extractall(component)
    return build, contract, wheel, manifest_hash


async def install_declared_dependencies(
    config: Path,
    manifest: dict[str, Any],
    observe: Callable[[str], None],
) -> None:
    from homeassistant.core import HomeAssistant
    from homeassistant.requirements import async_get_integration_with_requirements

    hass = HomeAssistant(str(config))
    visited: set[str] = set()
    try:
        for field, label in (
            ("dependencies", "dependency"),
            ("after_dependencies", "after_dependency"),
        ):
            for domain in manifest.get(field, []):
                if domain in visited:
                    continue
                visited.add(domain)
                await async_get_integration_with_requirements(hass, domain)
                await asyncio.to_thread(observe, f"manifest_{label}:{domain}")
    finally:
        await hass.async_stop()


def install(assets: Path, work: Path, output: Path) -> None:
    assert_isolated()
    proof = json.loads(output.read_text(encoding="utf-8"))
    config = work / "config"
    component = config / "custom_components/ha_mcp_tools"
    build, contract, wheel, manifest_hash = prepare_component(assets, component)
    manifest = json.loads((component / "manifest.json").read_bytes())
    before = environment()
    if {"ha-mcp", "ha-mcp-dev"} & before["versions"].keys():
        raise RuntimeError("Minimal Core already contains HA-MCP")
    if before["versions"].get("certifi") != proof["old_certifi_seed"]:
        raise RuntimeError("The older compatible certifi baseline was not established")
    proof.update(
        before=before,
        source_revision=build["revision"],
        wheel_sha256=build["files"][contract["wheel"]],
        manifest_sha256=manifest_hash,
        manifest_requirements=manifest["requirements"],
        manifest_dependencies=manifest.get("dependencies", []),
        manifest_after_dependencies=manifest.get("after_dependencies", []),
        decisions=[],
    )
    save(output, proof)
    from homeassistant.requirements import pip_kwargs
    from homeassistant.util import package

    kwargs = pip_kwargs(str(config))

    def observe(label: str) -> None:
        current = environment()
        delta = changes(before, current)
        proof.setdefault("observations", []).append(
            {"phase": label, **current, **delta}
        )
        save(output, proof)
        if delta["changed_preinstalled"] or delta["new_issues"]:
            raise RuntimeError(
                "Native installation changed the Core baseline or added conflicts"
            )

    try:
        # Leave upgrade at Core's default; this is deliberately not pip's old
        # only-if-needed behaviour that concealed the public install regression.
        if not package.install_package(str(wheel), **kwargs):
            raise RuntimeError("Core refused the paired local wheel")
        importlib.invalidate_caches()
        observe("paired_wheel")
        for requirement in manifest["requirements"]:
            effective, substituted = paired_requirement(
                requirement, contract["dependency"], wheel
            )
            decision = {
                "requirement": requirement,
                "paired_local_substitution": substituted,
            }
            proof["decisions"].append(decision)
            save(output, proof)
            decision["result"] = install_requirement(effective, kwargs)
            importlib.invalidate_caches()
            observe(f"manifest_requirement_{len(proof['decisions'])}")
        asyncio.run(install_declared_dependencies(config, manifest, observe))
        from .minimal_probes import run_probes

        proof["probes"] = asyncio.run(run_probes(config))
        observe("after_runtime_probes")
        proof["complete"] = True
        save(output, proof)
    except BaseException as error:
        proof["failure"] = {
            "type": type(error).__name__,
            "message": sanitize(str(error)),
        }
        try:
            current = environment()
            proof["failure_environment"] = {**current, **changes(before, current)}
        except (
            OSError,
            ValueError,
            RuntimeError,
            TypeError,
            SubprocessError,
        ) as inspection_error:
            proof["failure_environment_error"] = type(inspection_error).__name__
        save(output, proof)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    install(args.assets.resolve(), args.work_directory.resolve(), args.output.resolve())


if __name__ == "__main__":
    main()
