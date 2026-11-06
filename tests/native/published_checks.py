"""Check the installation environment at both ends of the Core bench."""

import json
from pathlib import Path
from typing import Any


async def exercise_installation(
    checks: Any, *, published_tag: str | None, phase: str
) -> None:
    mode = "published" if published_tag else "candidate"
    proof = await checks.control("dependency_receipt?phase=" + phase)
    # Preserve failed dependency evidence too, separately from partial tool checks.
    output = Path(__file__).parent / "out" / f"{mode}-install.json"
    output.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    assert proof["mode"] == mode, proof.get("mode")
    if published_tag:
        assert proof["release"]["tag"] == published_tag, proof["release"]
    observation = proof["observations"][-1]
    assert observation["verified"], {
        key: observation.get(key)
        for key in (
            "changed_preinstalled",
            "compatible_upgrades",
            "rejected_changes",
            "new_issues",
            "constraint_violations",
            "new_constraint_violations",
            "baseline_clean",
            "policy_errors",
            "errors",
            "receipt",
            "payload",
        )
    }
    assert not {"ha-mcp", "ha-mcp-dev"} & proof["before"].keys(), proof["before"]
    checks.passed(f"{mode}_Core_install_dependencies_{phase}")
