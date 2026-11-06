# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///

"""Container entrypoint. Creates only disposable config; preserves it on restart."""

import json
import os
import shutil
import sys
from pathlib import Path


def prepare(config: Path) -> None:
    config.mkdir(parents=True, exist_ok=True)
    storage = config / ".storage"
    storage.mkdir(exist_ok=True)
    entries = storage / "core.config_entries"
    if not entries.exists():
        # Core's automatic selection excludes loopback. In --network none that
        # leaves no adapter, and its DNS resolver tries a nonexistent default
        # multicast interface. Select the real lo through Core's own store.
        (storage / "core.network").write_text(
            json.dumps(
                {
                    "version": 1,
                    "minor_version": 1,
                    "key": "core.network",
                    "data": {"configured_adapters": ["lo"]},
                }
            ),
            encoding="utf-8",
        )
        shutil.copytree("/opt/native/custom_components", config / "custom_components")
        sys.path.insert(
            0, str(config / "custom_components" / "qualification_hacs_fixture")
        )
        from content import seed_repositories

        (config / "configuration.yaml").write_text(
            "homeassistant:\n  name: Isolated HACS qualification\n  latitude: 0\n"
            "  longitude: 0\n  elevation: 0\n  unit_system: metric\n  time_zone: UTC\n"
            "  auth_providers:\n    - type: homeassistant\n"
            "http:\nconfig:\nfrontend:\nlovelace:\n  mode: storage\n"
            "logger:\n  default: warning\nsystem_log:\nnative_fixture:\nqualification_hacs_fixture:\n",
            encoding="utf-8",
        )
        entries.write_text(
            json.dumps(
                {
                    "version": 1,
                    "minor_version": 1,
                    "key": "core.config_entries",
                    "data": {
                        "entries": [
                            {
                                "entry_id": "11111111111111111111111111111111",
                                "version": 1,
                                "minor_version": 1,
                                "domain": "hacs",
                                "title": "HACS 2.0.5 offline qualification",
                                "source": "user",
                                "unique_id": "hacs",
                                "data": {"token": "offline-fixture-not-a-token"},
                                "options": {},
                                "disabled_by": "user",
                                "discovery_keys": {},
                                "pref_disable_new_entities": False,
                                "pref_disable_polling": False,
                                "created_at": "2026-01-01T00:00:00+00:00",
                                "modified_at": "2026-01-01T00:00:00+00:00",
                            }
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        (storage / "hacs.repositories").write_text(
            json.dumps(
                {
                    "version": "6",
                    "minor_version": 1,
                    "key": "hacs.repositories",
                    "data": seed_repositories(),
                }
            ),
            encoding="utf-8",
        )
        preferences = config / ".ha_mcp"
        preferences.mkdir()
        (preferences / "feature_flags.json").write_text(
            json.dumps(
                {
                    "enable_tool_search": False,
                    "enable_mandatory_bps": False,
                }
            ),
            encoding="utf-8",
        )
    # Prevent the restored entry starting before the network fixture after a
    # real process restart. The fixture re-enables it through Core lifecycle.
    state = json.loads(entries.read_text(encoding="utf-8"))
    for entry in state["data"]["entries"]:
        if entry["domain"] == "hacs":
            entry["disabled_by"] = "user"
    entries.write_text(json.dumps(state), encoding="utf-8")


if __name__ == "__main__":
    prepare(Path("/config"))
    os.execv(
        sys.executable, [sys.executable, "-m", "homeassistant", "--config", "/config"]
    )
