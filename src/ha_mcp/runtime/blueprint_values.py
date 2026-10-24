"""Shared blueprint display parsing, path normalization and result projections."""

from __future__ import annotations

import logging
from typing import Any

import yaml  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class _BlueprintLoader(yaml.SafeLoader):
    """SafeLoader for blueprint text: keep ``!input``, neutralize all other tags."""


def _construct_input(loader: Any, node: Any) -> dict[str, str]:
    """Represent ``!input <name>`` as ``{"__input__": <name>}``."""
    return {"__input__": str(getattr(node, "value", ""))}


def _drop_tag(loader: Any, tag_suffix: Any, node: Any) -> None:
    """Neutralize every non-``!input`` custom tag to ``None``, never resolving it."""
    return None


_BlueprintLoader.add_constructor("!input", _construct_input)
_BlueprintLoader.add_multi_constructor("!", _drop_tag)


def parse_blueprint_body(text: str) -> dict[str, Any] | None:
    """Parse blueprint YAML into a display body, or ``None`` when it will not parse.

    Mirrors the component's ``_BlueprintLoader`` exactly so the same file yields
    the same body whichever rung of the ladder produced the text: ``!input`` is
    preserved as a JSON-safe marker and every other custom tag (``!secret`` /
    ``!include`` / …) resolves to ``None``, so no secret plaintext can enter the
    parsed body.
    """
    # Instance form (not yaml.load) mirrors the component's own loader usage;
    # _BlueprintLoader is a SafeLoader subclass, so no !!python/object tag can
    # construct an arbitrary type.
    loader = _BlueprintLoader(text)
    try:
        parsed = loader.get_single_data()
    except (ValueError, yaml.YAMLError) as exc:
        logger.debug("Blueprint body did not parse: %r", exc)
        return None
    finally:
        loader.dispose()
    return parsed if isinstance(parsed, dict) else None


def normalize_blueprint_path(path: str) -> str:
    """Return the path ``blueprint/save`` will actually write.

    Core's ``ws_save_blueprint`` appends ``.yaml`` when the path does not end
    with it — ``.yaml`` exactly, so ``foo.yml`` becomes ``foo.yml.yaml``. Every
    write derives its snapshot key and its reported path from this, so a save
    to ``user/motion`` captures and reports ``user/motion.yaml``: the file it
    replaces and the key the store will list, rather than a path no tier can
    read and the store never had.
    """
    return path if path.endswith(".yaml") else f"{path}.yaml"


def blueprint_list_result(
    blueprints_data: dict[str, Any], domain: str
) -> dict[str, Any]:
    """Format blueprint data into list response structure.

    Args:
        blueprints_data: Raw blueprint data from WebSocket API
        domain: Blueprint domain (automation or script)

    Returns:
        The canonical ``{"success": True, "data": {...}}`` envelope
        carrying the blueprints list, count, and domain.
    """
    blueprints = []
    for bp_path, metadata in blueprints_data.items():
        blueprint_info = {
            "path": bp_path,
            "domain": domain,
            "name": metadata.get("name", bp_path.split("/")[-1].replace(".yaml", "")),
        }

        # Add optional metadata if available
        if "metadata" in metadata:
            meta = metadata["metadata"]
            blueprint_info.update(
                {
                    "description": meta.get("description"),
                    "source_url": meta.get("source_url"),
                    "author": meta.get("author"),
                }
            )

        blueprints.append(blueprint_info)

    return {
        "success": True,
        "data": {
            "domain": domain,
            "count": len(blueprints),
            "blueprints": blueprints,
        },
    }


def blueprint_get_result(
    domain: str, path: str, entry: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """Assemble metadata without presenting it as an installed file's contents."""
    data: dict[str, Any] = {
        "path": path,
        "domain": domain,
        "name": entry.get(
            "name", path.rsplit("/", maxsplit=1)[-1].replace(".yaml", "")
        ),
    }
    result: dict[str, Any] = {"success": True, "data": data}
    source_url = None
    if "metadata" in entry:
        metadata = entry["metadata"]
        data["metadata"] = {
            key: metadata.get(key)
            for key in (
                "name",
                "description",
                "source_url",
                "author",
                "domain",
                "homeassistant",
            )
        }
        if "input" in metadata:
            data["inputs"] = metadata["input"]
        raw_source = metadata.get("source_url")
        source_url = raw_source if isinstance(raw_source, str) else None
    return result, source_url


def blueprint_saved_result(
    domain: str, path: str, overrides_existing: bool
) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "domain": domain,
            "path": path,
            "overrides_existing": overrides_existing,
            "message": (
                "Blueprint saved over the existing file. Automations and "
                "scripts using it were reloaded."
                if overrides_existing
                else "Blueprint saved."
            ),
        },
    }


def blueprint_imported_result(
    url: str, path: str, metadata: dict[str, Any], overrides_existing: bool
) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "url": url,
            "imported_blueprint": {
                "path": path,
                "domain": metadata.get("domain", "automation"),
                "name": metadata.get("name"),
                "description": metadata.get("description"),
            },
            "overrides_existing": overrides_existing,
            "message": (
                "Blueprint re-imported successfully. Automations/scripts using "
                "it were reloaded."
                if overrides_existing
                else "Blueprint imported successfully. Use "
                'ha_manage_blueprints(action="list") to see all installed '
                "blueprints."
            ),
        },
    }
