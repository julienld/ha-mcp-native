"""Installation provenance and release evidence must precede self mutation."""

from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp.self_update import InstallationEvidence, identify_installation
from ha_mcp.self_update_releases import assess_releases


@pytest.mark.parametrize(
    "evidence,method",
    [
        ({"embedded": True, "addon": True, "container": True}, "component"),
        ({"addon": True, "container": True}, "app"),
        ({"container": True, "installer": "pip"}, "docker"),
        ({"editable": True, "installer": "uv"}, "source"),
        ({"pipx_receipt": True, "installer": "pip"}, "pipx"),
        ({"uv_receipt": True}, "uv"),
        ({"uv_cache": True, "installer": "uv"}, "uv"),
        ({"installer": "pip"}, "pip"),
        ({"distributions": ("ha-mcp", "ha-mcp-dev"), "installer": "pip"}, "unknown"),
        ({"distributions": ()}, "unknown"),
    ],
)
def test_owner_evidence_overrides_nested_python_environment(
    evidence: dict[str, Any], method: str
) -> None:
    observed = identify_installation(
        InstallationEvidence(**{"distributions": ("ha-mcp",), **evidence})
    )
    assert observed.method == method
    if len(evidence.get("distributions", ("ha-mcp",))) != 1:
        assert observed.distribution is None


def releases(*values: tuple[str, str]) -> list[Any]:
    return [SimpleNamespace(tag_name=tag, body=body) for tag, body in values]


def test_missing_intermediate_notes_cannot_be_declared_compatible() -> None:
    result = assess_releases(
        "7.0.0", "7.2.0", releases(("7.0.0", "old"), ("7.1.0", ""), ("7.2.0", "fix"))
    )
    assert result["classification"] == "unknown"


def test_only_offered_interval_is_reviewed_including_intermediate_breaking_changes() -> (
    None
):
    result = assess_releases(
        "7.0.0",
        "7.2.0",
        releases(
            ("7.3.0", "future"),
            ("7.2.0", "fix"),
            ("7.1.0", "BREAKING CHANGES: Authentication changed"),
            ("7.0.0", "old"),
        ),
    )
    assert result["classification"] == "breaking"
    assert result["versions_checked"] == ["7.1.0", "7.2.0"]


def test_complete_regular_release_interval_needs_no_routine_confirmation() -> None:
    result = assess_releases(
        "7.0.0", "7.1.0", releases(("7.1.0", "Bug fixes"), ("7.0.0", "old"))
    )
    assert result["classification"] == "compatible"
    assert "not a guarantee" in result["basis"]


@pytest.mark.parametrize(
    "notes,classification",
    [
        ("The authentication changed; clients must migrate.", "breaking"),
        ("Bug fixes. No breaking changes.", "compatible"),
    ],
)
def test_contract_changes_dominate_missing_breaking_heading(
    notes: str, classification: str
) -> None:
    assert (
        assess_releases("7.0.0", "7.1.0", releases(("7.1.0", notes), ("7.0.0", "old")))[
            "classification"
        ]
        == classification
    )


def test_detected_auth_or_data_change_overrides_even_unchanged_release() -> None:
    result = assess_releases(
        "7.0.0", "7.0.0", [], detected_changes=["authentication", "data"]
    )
    assert result["classification"] == "breaking"


@pytest.mark.parametrize(
    "installed,offered,classification",
    [
        ("7.0.0", "8.0.0", "breaking"),
        ("7.2.0", "7.1.0", "breaking"),
        ("7.0.0", "unknown", "unknown"),
        ("7.0.0", "7.1.0.dev1", "unknown"),
    ],
)
def test_unreviewed_release_boundaries_are_not_regular_updates(
    installed: str, offered: str, classification: str
) -> None:
    assert assess_releases(installed, offered, [])["classification"] == classification
