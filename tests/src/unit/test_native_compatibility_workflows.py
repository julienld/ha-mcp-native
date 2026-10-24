"""The promised Core minimum must actually run in the two-architecture matrix."""

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_installation_and_tool_suites_cover_the_declared_minimum_and_three_months():
    constants = ast.parse(
        (ROOT / "custom_components/ha_mcp_tools/const.py").read_text(encoding="utf-8")
    )
    minimum = next(
        node.value.value
        for node in constants.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "MIN_NATIVE_HOME_ASSISTANT_VERSION"
            for target in node.targets
        )
    )
    matrices = []
    for name, job in [
        ("native-e2e.yml", "native-e2e"),
        ("native-migration.yml", "package"),
    ]:
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows" / name).read_text(encoding="utf-8")
        )
        selected = workflow["jobs"][job]
        matrix = selected["strategy"]["matrix"]
        versions = matrix["core"]
        assert minimum in versions, "The advertised minimum is not exercised"
        months = sorted(
            int(v.split(".")[0]) * 12 + int(v.split(".")[1]) for v in versions
        )
        assert len(months) == 3 and months == list(range(months[0], months[0] + 3))
        assert set(matrix["os"]) == {"ubuntu-24.04", "ubuntu-24.04-arm"}
        assert not any(key.startswith("python") for key in matrix)
        assert selected["runs-on"] == "${{ matrix.os }}"
        assert "${{ matrix.core }}" in selected["env"]["HA_IMAGE_GHCR"]
        matrices.append(versions)
    assert matrices[0] == matrices[1], (
        "Installability and tool parity cover different Cores"
    )
