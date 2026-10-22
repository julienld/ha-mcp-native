"""Entity state retrieval for ``ha_get_state``.

Bulk-result helpers and the single, bulk and component state methods.
"""

import asyncio
import logging
from typing import Any

from ...client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ...client.websocket_client import get_websocket_client
from ...runtime.states import StateTools
from ...runtime.states import _accumulate_state_results as _accumulate_state_results
from ...runtime.states import _build_bulk_states_response as _build_bulk_states_response
from ..component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from ..helpers import (
    exception_to_structured_error,
)
from ..response_helpers import add_timezone_metadata
from .base import SearchToolsBase

logger = logging.getLogger(__name__)


def _missing_entity_exc(entity_id: str) -> HomeAssistantAPIError:
    """A synthetic 404 for an id the component's ``states`` read reports absent.

    Classifying a component-reported miss through the same
    ``exception_to_structured_error`` path the legacy per-id REST 404 uses makes
    the missing-id error byte-identical on both backends (ENTITY_NOT_FOUND with
    the entity_id context; the response-level ``ha_search()`` suggestion still
    fires), without the server issuing a REST call it just avoided.
    """
    return HomeAssistantAPIError(
        f"API error: 404 - Entity {entity_id} not found", status_code=404
    )


class StateMixin(StateTools, SearchToolsBase):
    """Fetches entity states for ``ha_get_state``."""

    async def _timezone(self, data: dict[str, Any]) -> dict[str, Any]:
        return await add_timezone_metadata(self._client, data)

    async def _fetch_single_state(self, eid: str) -> dict[str, Any]:
        """Fetch state for one entity; returns structured error dict on failure."""
        try:
            state = await self._client.get_entity_state(eid)
            return {"success": True, "entity_id": eid, "state": state}
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to fetch state for '{eid}': {e}")
            # ast-grep-ignore — batch item failure, aggregated via asyncio.gather
            return exception_to_structured_error(
                e,
                context={"entity_id": eid},
                raise_error=False,
            )

    async def _get_one_state(self, entity_id: str) -> dict[str, Any]:
        """Return one entity's raw state dict — component bulk-read or legacy REST.

        The same fetch primitive the bulk path uses, so single- and bulk-mode
        ``ha_get_state`` share one code path. When the component serves it, an id
        it authoritatively reports absent raises the same 404 the legacy REST read
        would, so the caller's exception handler produces the identical
        single-entity ENTITY_NOT_FOUND (with its ``ha_search()`` suggestion).
        """
        component = await self._fetch_states_via_component([entity_id])
        if component is None:
            legacy_state: dict[str, Any] = await self._client.get_entity_state(
                entity_id
            )
            return legacy_state
        states = component.get("states") or {}
        if entity_id in states:
            record: dict[str, Any] = states[entity_id]
            return record
        raise _missing_entity_exc(entity_id)

    async def _resolve_bulk_state_results(
        self, unique_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Per-id fetch results for a bulk read — component bulk-read or legacy REST.

        Returns one entry per id in ``_fetch_single_state`` shape (a hit dict or a
        structured error). The component path resolves every id in ONE
        ``ha_mcp_tools/states`` frame instead of up to 100 REST GETs; a missing id
        is mapped to the same 404-classified error the legacy per-id path yields,
        so ``_accumulate_state_results`` and the response-level ``ha_search()``
        suggestion behave identically on both backends.
        """
        component = await self._fetch_states_via_component(unique_ids)
        if component is None:
            return list(
                await asyncio.gather(
                    *(self._fetch_single_state(eid) for eid in unique_ids)
                )
            )
        states = component.get("states") or {}
        return [self._component_state_result(eid, states) for eid in unique_ids]

    def _component_state_result(
        self, entity_id: str, states: dict[str, Any]
    ) -> dict[str, Any]:
        """One found/missing per-id result from the component's ``states`` map."""
        if entity_id in states:
            return {"success": True, "entity_id": entity_id, "state": states[entity_id]}
        # ast-grep-ignore — batch item failure, mapped to the legacy 404 shape
        return exception_to_structured_error(
            _missing_entity_exc(entity_id),
            context={"entity_id": entity_id},
            raise_error=False,
        )

    async def _fetch_states_via_component(
        self, entity_ids: list[str]
    ) -> dict[str, Any] | None:
        """One ``ha_mcp_tools/states`` bulk read; ``None`` ⇒ use the legacy REST path.

        Returns the component's ``{states, missing}`` payload (found ids mapped to
        their ``State.as_dict()`` body) or ``None`` when the component lacks the
        ``states`` capability, was downgraded (``unknown_command`` → invalidate the
        cached caps), or errored (logged). Falls back **silently** — unlike
        ``ha_search`` / ``ha_get_zone`` which append a ``warnings[]`` entry —
        because ``ha_get_state``'s single- and bulk-mode responses do not share one
        warnings channel; the ``log.warning`` preserves operator visibility and the
        legacy REST path returns the byte-identical correct data either way.
        ``ha_get_state``'s legacy path is a REST ``get_entity_state`` read on a
        SEPARATE transport, so a WS transport/connect failure
        (``HomeAssistantConnectionError``, covering both a pooled-WS drop and a
        failed connect) is caught here and falls back to REST — an install whose REST API
        still works keeps getting its state instead of a spurious connection error.
        If REST is also down, the legacy path raises the same connection error
        itself. (``ha_search`` / ``ha_get_overview`` likewise fall back on a
        transport failure — no component fetch helper propagates one.)
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "states"):
            return None
        try:
            raw = await self._send_component_states(entity_ids)
        except (
            HomeAssistantCommandError,
            HomeAssistantCommandTimeout,
            HomeAssistantConnectionError,
        ) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            else:
                logger.warning(
                    "ha_mcp_tools/states failed; fell back to legacy: %r", exc
                )
            return None
        except Exception as exc:  # noqa: BLE001
            # Anything the tuple above doesn't cover → legacy REST, so an
            # install whose REST API still works keeps answering.
            logger.warning(
                "ha_mcp_tools/states connection error; fell back to legacy: %r", exc
            )
            return None
        result = raw.get("result") or {}
        if not isinstance(result.get("states"), dict):
            return None
        return result

    async def _send_component_states(self, entity_ids: list[str]) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/states`` command over the per-client WebSocket."""
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        return await ws.send_command("ha_mcp_tools/states", entity_ids=entity_ids)
