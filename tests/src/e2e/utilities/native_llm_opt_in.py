"""Opt into the real native Assist bridge for one disposable schema test."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any

import requests

from tests.qualification_artifacts import native_runtime_selected
from tests.test_constants import TEST_TOKEN


def _headers(info: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {info.get('token', TEST_TOKEN)}"}


def _probe(info: dict[str, Any]) -> dict[str, Any]:
    response = requests.get(
        info["base_url"] + "/api/native_qualification?llm=1",
        headers=_headers(info),
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _set_export(info: dict[str, Any], entry_id: str, enabled: bool) -> None:
    endpoint = info["base_url"] + "/api/config/config_entries/options/flow"
    response = requests.post(
        endpoint, headers=_headers(info), json={"handler": entry_id}, timeout=60
    )
    response.raise_for_status()
    form = response.json()
    assert form["type"] == "form", form
    values = {}
    for field in form["data_schema"]:
        if "default" in field:
            values[field["name"]] = field["default"]
        elif "suggested_value" in field.get("description", {}):
            values[field["name"]] = field["description"]["suggested_value"]
    values["enable_llm_api"] = enabled
    saved = requests.post(
        endpoint + "/" + form["flow_id"],
        headers=_headers(info),
        json=values,
        timeout=60,
    )
    saved.raise_for_status()
    assert saved.json()["type"] == "create_entry", saved.json()


def assert_native_llm_registration(
    info: dict[str, Any], *, enabled: bool = True
) -> dict[str, Any]:
    """Observe Core's actual API registry, including teardown after opt-out."""
    deadline = time.monotonic() + 120
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = _probe(info)
            if (
                last.get("running")
                and last.get("llm_api_export_enabled") is enabled
                and bool(last.get("llm_api_ids")) is enabled
                and (
                    not enabled
                    or set(last.get("llm_api_types", [])) == {"NativeLlmApi"}
                )
            ):
                return last
        except requests.exceptions.RequestException as error:
            last = type(error).__name__
        time.sleep(1)
    raise AssertionError(
        f"Native LLM registration did not become enabled={enabled}: {last}"
    )


@contextmanager
def native_llm_opt_in(info: dict[str, Any]):
    if not native_runtime_selected():
        yield
        return
    before = _probe(info)
    enabled = bool(before["llm_api_export_enabled"])
    try:
        if not enabled:
            _set_export(info, before["entry_id"], True)
        assert_native_llm_registration(info)
        yield
    finally:
        if not enabled:
            _set_export(info, before["entry_id"], False)
            assert_native_llm_registration(info, enabled=False)
