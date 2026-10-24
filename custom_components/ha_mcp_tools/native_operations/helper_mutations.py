"""Call Core storage APIs; isolate the person validator that can suspend."""

from __future__ import annotations

from functools import partial
from typing import Any

from ha_mcp.runtime.mutation_guard import check_mutation_admission
from ha_mcp.tool_api import CallContext, ToolFailure

from ..helper_collections import async_write_item
from .owner import CoreOperationOwner
from .registry_expectation import check_expected_registry
from .storage_api import write_direct


class NativeHelperMutations(CoreOperationOwner):
    async def execute(
        self, arguments: dict[str, Any], actor: CallContext
    ) -> dict[str, Any]:
        from homeassistant.helpers import entity_registry as er

        invoke = write_direct
        if arguments["helper_type"] == "person":
            from .person_mutations import write_person

            invoke = write_person
        writer = partial(
            invoke,
            actor,
            self._admit,
            arguments.get("expected_item"),
            arguments.get("expected_registry"),
            self.hass,
        )
        if arguments["helper_type"] == "tag":
            from .tag_mutations import write_tag

            writer = partial(
                write_tag,
                actor,
                self._admit,
                arguments.get("expected_item"),
                check_registry=partial(
                    check_expected_registry,
                    self.hass,
                    arguments.get("expected_registry"),
                ),
            )
        return await async_write_item(
            self.hass,
            er.async_get(self.hass),
            arguments,
            writer=writer,
        )

    async def _admit(self, actor: CallContext) -> None:
        try:
            await self.authorize(actor)
            check_mutation_admission(actor)
        except PermissionError as error:
            raise ToolFailure(
                "AUTH_REQUIRED",
                "Administrator admission was revoked",
                status="not_started",
            ) from error
        except ToolFailure as error:
            raise ToolFailure(
                error.code, str(error), data=error.data, status="not_started"
            ) from error
        if actor.read_only:
            raise ToolFailure("READ_ONLY", "Writes are disabled", status="not_started")
