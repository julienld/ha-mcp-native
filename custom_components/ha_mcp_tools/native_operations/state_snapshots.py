"""The historical entity domain restores temporary StateMachine values only."""

from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import Context, HomeAssistant, State, validate_state
from homeassistant.exceptions import InvalidEntityFormatError, InvalidStateError

from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.config_hash import json_values_equal

from .registry_access import (
    Authorize,
    admit_registry_write,
    authorize_actor,
    registry_json,
)
from .registry_failures import registry_outcome_unknown

STATE_ONLY_WARNING = (
    "Legacy entity snapshots contain only state and attributes. This restore does not "
    "restore registry settings or physical device state. A state change can trigger "
    "automations, and an integration may publish its current state again."
)


@dataclass(slots=True)
class NativeStateSnapshots:
    hass: HomeAssistant
    actor: CallContext
    authorize: Authorize = field(kw_only=True, repr=False)
    states: Any = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.states = self.hass.states

    def _current(self, entity_id: str) -> dict[str, Any] | None:
        state = self.states.get(entity_id)
        return registry_json(state.as_dict()) if state is not None else None

    async def read_snapshot(self, entity_id: str) -> dict[str, Any] | None:
        await authorize_actor(self.actor, self.authorize)
        if self.hass.states is not self.states:
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The StateMachine owner changed",
                status="not_started",
            )
        return self._current(entity_id)

    async def restore_snapshot(
        self, entity_id: str, config: Any, expected: Any
    ) -> dict[str, Any]:
        await admit_registry_write(self.actor, self.authorize)
        context = Context(user_id=self.actor.user_id)
        if isinstance(config, dict) and config.get("state") is None:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Legacy entity snapshot does not specify a state",
                status="not_started",
            )
        try:
            proposed = State(
                entity_id,
                config.get("state") if isinstance(config, dict) else str(config),
                config.get("attributes", {}) if isinstance(config, dict) else {},
                context=context,
            )
            validate_state(proposed.state)
        except (
            InvalidEntityFormatError,
            InvalidStateError,
            TypeError,
            ValueError,
        ) as error:
            raise ToolFailure(
                "VALIDATION_INVALID_PARAMETER",
                "Legacy entity snapshot is not a valid Core state value",
                status="not_started",
            ) from error
        if self.hass.states is not self.states or not json_values_equal(
            self._current(entity_id), expected
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "The state changed after its safety image was read; no state was written.",
                status="not_started",
            )
        check_mutation_admission(self.actor)
        try:
            self.states.async_set(
                proposed.entity_id, proposed.state, proposed.attributes, context=context
            )
            written = self._current(proposed.entity_id)
            if (
                self.hass.states is not self.states
                or written is None
                or not json_values_equal(written.get("state"), proposed.state)
                or not json_values_equal(
                    written.get("attributes"),
                    registry_json({"value": proposed.attributes})["value"],
                )
            ):
                raise RuntimeError(
                    "Core did not confirm the requested state and attributes"
                )
            return {
                "state": written,
                "warnings": [STATE_ONLY_WARNING],
                "temporary_state_only": True,
            }
        except Exception as error:  # noqa: BLE001 - the StateMachine and listeners may already have changed
            registry_outcome_unknown(
                "entity_state",
                "restore",
                error,
                context={
                    "temporary_state_only": True,
                    "warnings": [STATE_ONLY_WARNING],
                },
            )
