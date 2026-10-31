"""Transport-neutral scene storage classification, preserving existing guidance."""

from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure


def raise_scene_config_missing(scene_id: str, result: dict[str, Any]) -> NoReturn:
    if result.get("not_in_storage"):
        _raise_scene_not_storage_error(scene_id, result.get("platform"))
    raise_failure(
        create_error_response(
            ErrorCode.ENTITY_NOT_FOUND,
            f"Entity 'scene.{scene_id.removeprefix('scene.')}' not found",
            context={
                "entity_id": f"scene.{scene_id.removeprefix('scene.')}",
                "scene_id": scene_id,
            },
            suggestions=[
                "Call ha_config_get_scene() without scene_id to discover scenes",
                "Check Home Assistant connection",
                "Use ha_get_skill_guide for help",
            ],
        )
    )


def _raise_scene_not_storage_error(scene_id: str, platform: str | None) -> NoReturn:
    """Raise ``CONFIG_NOT_FOUND`` for a scene that exists but has no editable config.

    The scene entity was confirmed to exist - in the entity registry, or, for an
    ``id``-less YAML scene that has no registry entry, in the state machine - but
    ``config/scene/config/{id}``, backed only by the managed ``scenes.yaml``, has
    no entry for it. That is the not-a-storage-scene case (a Hue/vendor scene, or
    a scene defined directly in YAML): the entity EXISTS, so ``ENTITY_NOT_FOUND``
    would be wrong (issue #1971). ``platform`` names the owning integration when
    the registry exposed it; the message stays generic otherwise.
    """
    entity_id = f"scene.{scene_id.removeprefix('scene.')}"
    suggestions = [
        f"Activate it with ha_config_set_scene(scene_id='{entity_id}', activate=True).",
    ]
    if platform and platform != "homeassistant":
        platform_note = (
            f" It is provided by the '{platform}' integration, which manages its "
            "own scenes outside Home Assistant's editable scene storage."
        )
        suggestions.append(
            f"Edit this scene in the '{platform}' integration or its companion app; "
            "ha_config_get_scene / ha_config_set_scene only edit Home Assistant "
            "UI/storage scenes."
        )
    else:
        platform_note = ""
        suggestions.append(
            "ha_config_get_scene / ha_config_set_scene only read and edit Home "
            "Assistant UI/storage scenes (Settings > Automations & Scenes > Scenes). "
            "Scenes defined directly in YAML are not editable through this API."
        )
    raise_failure(
        create_error_response(
            ErrorCode.CONFIG_NOT_FOUND,
            f"Scene '{entity_id}' exists but has no editable Home Assistant "
            f"storage configuration.{platform_note}",
            suggestions=suggestions,
            context={"scene_id": scene_id, "platform": platform},
        )
    )
