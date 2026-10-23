"""WebSocket compatibility envelope around the canonical Core service call."""

from typing import Any

from homeassistant.core import HomeAssistant

from ..core_call_service import (
    _LOGGER as _LOGGER,
)
from ..core_call_service import (
    _await_confirmation as _await_confirmation,
)
from ..core_call_service import (
    _build_call_service_result as _build_call_service_result,
)
from ..core_call_service import (
    _call_service_transition as _call_service_transition,
)
from ..core_call_service import (
    _confirmable_entity_ids as _confirmable_entity_ids,
)
from ..core_call_service import (
    _dispatched_unconfirmed_result as _dispatched_unconfirmed_result,
)
from ..core_call_service import (
    _event_state_value as _event_state_value,
)
from ..core_call_service import (
    _guard_call_service_target as _guard_call_service_target,
)
from ..core_call_service import (
    _match_immediate as _match_immediate,
)
from ..core_call_service import (
    _post_state as _post_state,
)
from ..core_call_service import (
    _register_transition_waiter as _register_transition_waiter,
)
from ..core_call_service import (
    _values_differ as _values_differ,
)
from ..core_call_service import (
    call_service as call_service,
)


async def _call_service_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    values = {
        key: value
        for key, value in msg.items()
        if key not in {"id", "type", "ha_mcp_context"}
    }
    return {
        "result": await call_service(hass, **values, context=msg.get("ha_mcp_context"))
    }


def _do_call_service(
    hass: HomeAssistant, params: dict[str, Any], *, result: dict[str, Any]
) -> dict[str, Any]:
    return result
