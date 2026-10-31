"""Runner console entry points must collect contracts without inherited sys.path."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("working_directory", [".", "tests"])
def test_public_contracts_collect_from_both_upstream_runner_working_directories(
    working_directory,
):
    root = Path(__file__).resolve().parents[3]
    executable = Path(sys.executable).parent / (
        "pytest.exe" if os.name == "nt" else "pytest"
    )
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            str(executable),
            str(root / "tests/src/e2e/continuity"),
            "--collect-only",
            "-q",
            "-o",
            "addopts=",
        ],
        cwd=root / working_directory,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "test_user_workflows.py::" in result.stdout
