"""Core's isolated installer preserves the ordinary cooldown and operator config."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.native import minimal_uv_policy


@pytest.fixture
def policy(tmp_path: Path) -> Path:
    source = tmp_path / "pyproject.toml"
    source.write_text('[tool.uv]\nexclude-newer = "7 days"\n', encoding="utf-8")
    return source


def test_native_installer_keeps_cooldown_without_writing_an_exception_config(
    policy: Path,
) -> None:
    original = {"VIRTUAL_ENV": "isolated", "UV_CONFIG_FILE": "operator.toml"}
    env, proof = minimal_uv_policy.native_install_environment(
        original, policy.parent, policy, now=datetime(2026, 10, 31, tzinfo=UTC)
    )
    assert env["UV_CONFIG_FILE"] == original["UV_CONFIG_FILE"]
    assert "UV_EXCLUDE_NEWER" not in original
    assert env["UV_EXCLUDE_NEWER"] and not proof["exception_active"]
    assert list(policy.parent.iterdir()) == [policy]


def test_installer_rejects_per_package_cooldown_overrides(policy: Path) -> None:
    policy.write_text(
        '[tool.uv]\nexclude-newer = "7 days"\nexclude-newer-package = {example = false}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exceptions"):
        minimal_uv_policy.native_install_environment(
            {}, policy.parent, policy, now=datetime(2026, 10, 31, tzinfo=UTC)
        )
