"""Schema qualification opts in explicitly and restores the prior bridge state."""

from types import SimpleNamespace

import pytest

from tests.src.e2e.utilities import native_llm_opt_in as probe


def test_failed_schema_probe_restores_opt_out_and_preserves_owner_and_import_mode(
    monkeypatch,
):
    monkeypatch.setenv("E2E_EMBEDDED_RUNTIME", "native")
    observations = iter(
        [
            {"entry_id": "server", "llm_api_export_enabled": False},
            {
                "running": True,
                "llm_api_export_enabled": True,
                "llm_api_ids": ["api"],
                "llm_api_types": ["NativeLlmApi"],
            },
            {"running": True, "llm_api_export_enabled": False, "llm_api_ids": []},
        ]
    )
    monkeypatch.setattr(probe, "_probe", lambda info: next(observations))
    submissions = []

    def post(url, *, headers, json, timeout):
        if url.endswith("/flow"):
            assert json == {"handler": "server"}
            body = {
                "type": "form",
                "flow_id": "f",
                "data_schema": [
                    {"name": "enable_native_llm_import", "default": False},
                    {
                        "name": "native_owner_user_id",
                        "description": {"suggested_value": "owner"},
                    },
                ],
            }
        else:
            submissions.append(json)
            body = {"type": "create_entry"}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    monkeypatch.setattr(probe.requests, "post", post)
    with (
        pytest.raises(RuntimeError, match="schema rejected"),
        probe.native_llm_opt_in({"base_url": "http://fixture", "token": "fixture"}),
    ):
        raise RuntimeError("schema rejected")
    assert [row["enable_llm_api"] for row in submissions] == [True, False]
    assert all(row["native_owner_user_id"] == "owner" for row in submissions)
    assert all(row["enable_native_llm_import"] is False for row in submissions)
