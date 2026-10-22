"""Run the managed-install proof at both ends of the unchanged Core bench."""

import json
from pathlib import Path
from typing import Any


async def exercise_published_install(checks: Any, *, tag: str, phase: str) -> None:
    proof = await checks.control("dependency_receipt")
    # Preserve failed dependency evidence too, separately from partial tool checks.
    output = Path(__file__).parent / "out" / "published-install.json"
    output.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    assert proof["mode"] == "published", proof
    assert proof["release"]["tag"] == tag, proof["release"]
    observation = proof["observations"][-1]
    assert observation["verified"], observation
    assert not {"ha-mcp", "ha-mcp-dev"} & proof["before"].keys(), proof["before"]
    checks.passed(f"published_Core_managed_install_receipt_and_dependencies_{phase}")
