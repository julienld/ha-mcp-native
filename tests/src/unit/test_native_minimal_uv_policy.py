"""Core's isolated installer must receive only the approved certifi exception."""

import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.native import minimal_uv_policy


@pytest.fixture
def policy(tmp_path: Path) -> Path:
    source = tmp_path / "pyproject.toml"
    source.write_text(
        '[tool.uv]\nexclude-newer = "7 days"\n'
        'exclude-newer-package = {certifi = "2026-10-21T00:00:00Z"}\n'
    )
    return source


def test_native_installer_receives_bounded_config_without_mutating_bootstrap_env(
    policy: Path,
) -> None:
    original = {"UV_EXCLUDE_NEWER": "7 days", "VIRTUAL_ENV": "isolated"}
    env, proof = minimal_uv_policy.native_install_environment(
        original, policy.parent, policy, now=datetime(2026, 10, 26, tzinfo=UTC)
    )
    assert "UV_CONFIG_FILE" not in original
    assert env["UV_EXCLUDE_NEWER"] == original["UV_EXCLUDE_NEWER"]
    config = tomllib.loads(Path(env["UV_CONFIG_FILE"]).read_text())
    assert config == {
        "pip": {"exclude-newer-package": {"certifi": "2026-10-21T00:00:00Z"}}
    }
    assert proof["exception_active"] and proof["config_sha256"]
    assert "UV_SYSTEM_CERTS" not in env


def test_expired_exception_cannot_keep_pinning_certifi_after_cooldown(
    policy: Path,
) -> None:
    env, proof = minimal_uv_policy.native_install_environment(
        {"UV_EXCLUDE_NEWER": "7 days"},
        policy.parent,
        policy,
        now=datetime(2026, 10, 27, 2, 24, 17, tzinfo=UTC),
    )
    assert "UV_CONFIG_FILE" not in env and not proof["exception_active"]
    assert env["UV_EXCLUDE_NEWER"] == "7 days"


@pytest.mark.parametrize(
    "exception",
    [
        "{certifi = false}",
        '{certifi = "2026-11-01T00:00:00Z"}',
        '{certifi = "2026-10-21T00:00:00Z", uv = "2026-10-21T00:00:00Z"}',
    ],
)
def test_exception_cannot_expand_to_later_releases_or_another_package(
    policy: Path, exception: str
) -> None:
    policy.write_text(
        f'[tool.uv]\nexclude-newer = "7 days"\nexclude-newer-package = {exception}\n'
    )
    with pytest.raises(ValueError, match="certifi"):
        minimal_uv_policy.native_install_environment(
            {}, policy.parent, policy, now=datetime(2026, 10, 26, tzinfo=UTC)
        )


def test_explicit_operator_config_cannot_be_silently_replaced(policy: Path) -> None:
    with pytest.raises(ValueError, match="configuration"):
        minimal_uv_policy.native_install_environment(
            {"UV_CONFIG_FILE": "operator.toml"},
            policy.parent,
            policy,
            now=datetime(2026, 10, 26, tzinfo=UTC),
        )
