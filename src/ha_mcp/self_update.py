"""Identify the current process's owner without inventing access to its host."""

import asyncio
import importlib.metadata
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ha_mcp import __version__
from ha_mcp._version import _owns_ha_mcp, is_embedded, is_running_in_addon
from ha_mcp.self_update_guides import installation_guides, update_guide
from ha_mcp.tool_api import ToolFailure


@dataclass(frozen=True)
class InstallationEvidence:
    embedded: bool = False
    addon: bool = False
    container: bool = False
    distributions: tuple[str, ...] = ()
    installer: str = ""
    editable: bool = False
    pipx_receipt: bool = False
    uv_receipt: bool = False
    uv_cache: bool = False
    source_checkout: bool = False
    direct_reference: bool = False


@dataclass(frozen=True)
class Installation:
    method: str
    distribution: str | None
    evidence: str
    runtime: str = "external"
    possible_methods: tuple[str, ...] = ()


def identify_installation(evidence: InstallationEvidence) -> Installation:
    owners = evidence.distributions
    distribution = owners[0] if len(owners) == 1 else None
    if evidence.embedded:
        return Installation("component", distribution, "embedded runtime", "legacy")
    if evidence.addon:
        return Installation("app", distribution, "Supervisor process context")
    if evidence.container:
        return Installation(
            "docker", distribution, "container marker; host control unavailable"
        )
    if not owners and evidence.source_checkout:
        return Installation("source", None, "package imported from a source checkout")
    if len(owners) != 1:
        return Installation(
            "unknown", None, "distribution ownership missing or ambiguous"
        )
    return _python_installation(evidence, distribution)


def _python_installation(
    evidence: InstallationEvidence, distribution: str | None
) -> Installation:
    if evidence.pipx_receipt:
        return Installation("pipx", distribution, "pipx environment receipt")
    if evidence.editable:
        return Installation("source", distribution, "editable distribution metadata")
    if evidence.uv_receipt:
        return Installation("uv", distribution, "uv tool receipt")
    if evidence.installer == "uv" and evidence.uv_cache:
        return Installation(
            "uv",
            distribution,
            "uv cached environment; the launch command is not observable",
            possible_methods=("uv", "uvx"),
        )
    if evidence.installer in {"pip", "uv"}:
        return Installation(
            evidence.installer, distribution, "distribution INSTALLER metadata"
        )
    return Installation("unknown", distribution, "installer has no recognized owner")


def _local_evidence() -> InstallationEvidence:
    owners = tuple(name for name in ("ha-mcp", "ha-mcp-dev") if _owns_ha_mcp(name))
    installer, editable, direct_reference = "", False, False
    if len(owners) == 1:
        dist = importlib.metadata.distribution(owners[0])
        installer = (dist.read_text("INSTALLER") or "").strip()
        try:
            direct = json.loads(dist.read_text("direct_url.json") or "{}")
            editable = direct.get("dir_info", {}).get("editable") is True
            direct_reference = bool(direct.get("url"))
        except (ValueError, AttributeError):
            # Malformed optional metadata cannot establish a direct/editable owner.
            pass
    prefix = Path(sys.prefix)
    return InstallationEvidence(
        embedded=is_embedded(),
        addon=is_running_in_addon(),
        container=Path("/.dockerenv").is_file() or Path("/run/.containerenv").is_file(),
        distributions=owners,
        installer=installer,
        editable=editable,
        pipx_receipt=(prefix / "pipx_metadata.json").is_file(),
        uv_receipt=(prefix / "uv-receipt.toml").is_file(),
        uv_cache=any(part.startswith("archive-v") for part in prefix.parts),
        source_checkout=(Path(__file__).parents[2] / "pyproject.toml").is_file()
        and (Path(__file__).parents[2] / ".git").exists(),
        direct_reference=direct_reference,
    )


class ProcessSelfUpdate:
    """External owners must replace/relaunch us from outside this process."""

    async def manage(self, action: str) -> dict[str, Any]:
        evidence = await asyncio.to_thread(_local_evidence)
        installation = identify_installation(evidence)
        result: dict[str, Any] = {
            "success": True,
            "installation": asdict(installation),
            "running_version": __version__,
            "installed_version": None,
            "available_version": None,
            "capability": "manual_required",
            "status": "not_started",
            "guide": update_guide(installation.method, installation.distribution),
            "limitations": [
                "No external installation owner with a verifiable completion acknowledgement is bound to this process.",
                "The startup update notice is cached and does not prove current installability or release compatibility.",
            ],
        }
        if evidence.direct_reference and installation.method in (
            "pip",
            "uv",
            "uvx",
            "pipx",
        ):
            result["guide"]["steps"] = [
                "This environment was installed from a direct artifact or source reference. On the server host, inspect the existing launcher/lock and upgrade that same origin after stopping the server.",
                "Do not replace its reference with an unqualified PyPI package upgrade. Preserve the distribution, channel, authentication and client configuration, then restart the existing entrypoint.",
            ]
        if action == "check" and (
            installation.method == "app"
            or (
                not evidence.direct_reference
                and installation.method not in ("component", "source")
            )
        ):
            from ha_mcp.update_check import get_update_field

            result["release_notice"] = await get_update_field()
        if action == "guide":
            result["installation_methods"] = installation_guides()
        if action == "update":
            raise ToolFailure(
                "UNSUPPORTED_OPERATION",
                "Update this installation through its owner using the returned guide.",
                data={
                    **result,
                    "success": False,
                    "error": {
                        "code": "UNSUPPORTED_OPERATION",
                        "message": "External installation owner required",
                    },
                },
                status="not_started",
            )
        return result
