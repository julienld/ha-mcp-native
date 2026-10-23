"""Text conversation dispatch using the actual Core conversation owner."""

import asyncio
import copy
from typing import Any

from homeassistant.core import Context

from ha_mcp.runtime.pipeline_values import (
    PipelineValues,
    process_result,
    validate_sentence,
)
from ha_mcp.tool_api import ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io
from ha_mcp.utils.registry_update_lock import registry_update_lock

from .command_validation import _validated_payload
from .registry_access import admit_registry_write, authorize_actor


class PipelineConversation:
    # The owning service supplies these; this mixin keeps the two Core owners
    # separate without creating a generic transport or operation dispatcher.
    hass: Any
    actor: Any
    authorize: Any

    async def _process_sentence(
        self,
        *,
        sentence: str | None,
        language: str | None,
        conversation_id: str | None,
        agent_id: str | None,
        pipeline_id: str | None,
    ) -> dict[str, Any]:
        from .pipeline_store import _get, pipeline_image, pipeline_owner

        validate_sentence(sentence)
        await admit_registry_write(self.actor, self.authorize)
        async with registry_update_lock("assist_pipeline", "pipelines"):
            owner = pipeline_owner(self.hass) if pipeline_id is not None else None
            before = pipeline_image(owner) if owner is not None else None
            if owner is not None:
                pipeline = _get(self.hass, pipeline_id)
                if agent_id is None:
                    agent_id = pipeline.get("conversation_engine")
                if language is None:
                    language = PipelineValues._pipeline_intent_language(pipeline)
            payload = {
                "text": sentence,
                **{
                    key: value
                    for key, value in (
                        ("language", language),
                        ("conversation_id", conversation_id),
                        ("agent_id", agent_id),
                    )
                    if value is not None
                },
            }
            manager, component, agent = self._conversation_owner(agent_id)
            registration = self.hass.data.get("websocket_api", {}).get(
                "conversation/process"
            )
            payload = _validated_payload(self.hass, "conversation/process", payload)
        # A conversation agent can invoke pipeline tools itself. Release the
        # preparation mutex before entering that agent; the owned worker checks
        # every frozen owner/image again immediately before dispatch.
        return await await_owned_io(
            self._converse(
                owner,
                before,
                manager,
                component,
                agent,
                registration,
                payload,
                pipeline_id,
            )
        )

    def _conversation_owner(self, agent_id: str | None) -> tuple[Any, Any, Any]:
        if "conversation" not in self.hass.config.components:
            raise ToolFailure(
                "SERVICE_UNAVAILABLE",
                "Core conversation is not loaded",
                status="not_started",
            )
        from homeassistant.components.conversation.agent_manager import async_get_agent
        from homeassistant.components.conversation.const import DATA_COMPONENT

        manager = self.hass.data.get("conversation_agent")
        component = self.hass.data.get(DATA_COMPONENT)
        if manager is None or isinstance(manager, asyncio.Future) or component is None:
            raise ToolFailure(
                "SERVICE_UNAVAILABLE",
                "Core conversation is not loaded yet",
                status="not_started",
            )
        agent = async_get_agent(self.hass, agent_id)
        if agent is None:
            raise ToolFailure(
                "NOT_FOUND", "Conversation agent is unavailable", status="not_started"
            )
        return manager, component, agent

    def _pin_conversation(
        self,
        owner: Any,
        before: Any,
        manager: Any,
        component: Any,
        agent: Any,
        registration: Any,
        agent_id: str | None,
    ) -> None:
        from .pipeline_store import pin_pipeline

        current = self._conversation_owner(agent_id)
        if (
            any(
                left is not right
                for left, right in zip(
                    current, (manager, component, agent), strict=True
                )
            )
            or self.hass.data.get("websocket_api", {}).get("conversation/process")
            is not registration
        ):
            raise ToolFailure(
                "CONFIG_CONFLICT",
                "Core conversation owner changed",
                status="not_started",
            )
        if owner is not None:
            pin_pipeline(self.hass, owner, before)

    async def _converse(
        self,
        owner: Any,
        before: Any,
        manager: Any,
        component: Any,
        agent: Any,
        registration: Any,
        payload: dict[str, Any],
        pipeline_id: str | None,
    ) -> dict[str, Any]:
        from homeassistant.components.conversation.agent_manager import async_converse

        await admit_registry_write(self.actor, self.authorize)
        self._pin_conversation(
            owner,
            before,
            manager,
            component,
            agent,
            registration,
            payload.get("agent_id"),
        )
        try:
            result = await async_converse(
                self.hass,
                text=payload["text"],
                conversation_id=payload.get("conversation_id"),
                context=Context(user_id=self.actor.user_id),
                language=payload.get("language"),
                agent_id=payload.get("agent_id"),
            )
            await authorize_actor(self.actor, self.authorize)
            self._pin_conversation(
                owner,
                before,
                manager,
                component,
                agent,
                registration,
                payload.get("agent_id"),
            )
            return process_result(
                copy.deepcopy(result.as_dict()),
                pipeline_id=pipeline_id,
                agent_id=payload.get("agent_id"),
                language=payload.get("language"),
            )
        except (Exception, asyncio.CancelledError) as error:
            raise ToolFailure(
                "OUTCOME_UNKNOWN",
                "Core conversation was dispatched and may have acted. Its final outcome is uncertain; inspect Home Assistant before issuing another command.",
                status="outcome_unknown",
            ) from error
