"""A real Core conversation agent and pipeline probes for the native CI bench.

Root fixture hooks: POST pipeline_seed calls seed_pipeline_fixture(hass); GET
pipeline_probe returns pipeline_probe(hass); POST pipeline_cleanup calls
await cleanup_pipeline(hass, body["pipeline_id"], body["preferred_pipeline"]).
"""

from typing import Any

from homeassistant.components.assist_pipeline.pipeline import KEY_ASSIST_PIPELINE
from homeassistant.components.conversation.agent_manager import get_agent_manager
from homeassistant.components.conversation.models import (
    AbstractConversationAgent,
    ConversationInput,
    ConversationResult,
)
from homeassistant.helpers.intent import IntentResponse

_AGENT_ID = "native_pipeline_agent"
_DATA = "native_pipeline_probe"


class PipelineFixtureAgent(AbstractConversationAgent):
    def __init__(self, hass: Any) -> None:
        self.hass = hass

    @property
    def supported_languages(self) -> list[str]:
        return ["en"]

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        self.hass.data[_DATA].append(
            {
                "text": user_input.text,
                "user_id": user_input.context.user_id,
                "language": user_input.language,
                "conversation_id": user_input.conversation_id,
            }
        )
        await self.hass.services.async_call(
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.native_light"},
            blocking=True,
            context=user_input.context,
        )
        response = IntentResponse(language=user_input.language)
        response.async_set_speech("Native Assist command completed")
        return ConversationResult(
            response=response,
            conversation_id=user_input.conversation_id
            or "native-pipeline-conversation",
        )


def seed_pipeline_fixture(hass: Any) -> None:
    assert KEY_ASSIST_PIPELINE in hass.data
    assert "conversation_agent" in hass.data
    hass.data[_DATA] = []
    get_agent_manager(hass).async_set_agent(_AGENT_ID, PipelineFixtureAgent(hass))


def pipeline_probe(hass: Any) -> dict[str, Any]:
    owner = hass.data[KEY_ASSIST_PIPELINE].pipeline_store
    state = hass.states.get("input_boolean.native_light")
    return {
        "pipelines": [row.to_json() for row in owner.data.values()],
        "preferred_pipeline": owner.async_get_preferred_item(),
        "calls": list(hass.data[_DATA]),
        "state": state.state,
        "state_user_id": state.context.user_id,
        "agent_id": _AGENT_ID,
    }


async def cleanup_pipeline(
    hass: Any, pipeline_id: str, preferred_pipeline: str
) -> None:
    owner = hass.data[KEY_ASSIST_PIPELINE].pipeline_store
    owner.async_set_preferred_item(preferred_pipeline)
    if pipeline_id in owner.data:
        await owner.async_delete_item(pipeline_id)
