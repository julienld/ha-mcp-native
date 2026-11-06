"""Shared Assist tool signature and orchestration, without a transport owner."""

from typing import Annotated, Any, Literal, Protocol

from pydantic import Field

from ha_mcp.tool_api import ToolFailure

from .errors import raise_classified
from .pipeline_values import PipelineValues

PipelineAction = Literal["list", "get", "create", "update", "set_preferred", "process"]


class PipelineService(Protocol):
    async def _manage_pipeline_read(
        self, *, action: str, pipeline_id: str | None
    ) -> dict[str, Any]: ...
    async def _set_preferred_pipeline(
        self, pipeline_id: str | None
    ) -> dict[str, Any]: ...
    async def _process_sentence(
        self,
        *,
        sentence: str | None,
        language: str | None,
        conversation_id: str | None,
        agent_id: str | None,
        pipeline_id: str | None,
    ) -> dict[str, Any]: ...
    async def _write_pipeline(
        self,
        *,
        action: str,
        pipeline_id: str | None,
        base_pipeline_id: str | None,
        updates: dict[str, Any],
        make_preferred: bool,
    ) -> dict[str, Any]: ...


class PipelineTools(PipelineValues):
    def __init__(self, pipelines: PipelineService) -> None:
        self.pipelines = pipelines

    async def ha_manage_pipeline(
        self,
        action: Annotated[
            PipelineAction,
            Field(
                description=(
                    "Pipeline operation: list, get, create, update, set_preferred, "
                    "or process."
                ),
            ),
        ],
        pipeline_id: Annotated[
            str | None,
            Field(
                description=(
                    "Assist pipeline ID. Required for get, update, and set_preferred. "
                    "Optional for process, where it selects the conversation agent and "
                    "language that pipeline is configured with."
                ),
                default=None,
            ),
        ] = None,
        sentence: Annotated[
            str | None,
            Field(
                description=(
                    "Natural-language command to run through Assist. Required when "
                    "action='process'."
                ),
                default=None,
            ),
        ] = None,
        conversation_id: Annotated[
            str | None,
            Field(
                description=(
                    "For process only, the conversation to continue. Returned in the "
                    "response so follow-up sentences keep their context."
                ),
                default=None,
            ),
        ] = None,
        agent_id: Annotated[
            str | None,
            Field(
                description=(
                    "For process only, the conversation agent entity ID to answer, "
                    "e.g. 'conversation.home_assistant'. Overrides the agent taken "
                    "from pipeline_id; omit both for the default agent."
                ),
                default=None,
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                description="Pipeline display name. Required when action='create'.",
                default=None,
            ),
        ] = None,
        conversation_engine: Annotated[
            str | None,
            Field(
                description=(
                    "Conversation agent entity ID or engine ID. Required when action='create'."
                ),
                default=None,
            ),
        ] = None,
        base_pipeline_id: Annotated[
            str | None,
            Field(
                description=(
                    "Pipeline ID to clone when creating. Omit to clone the preferred "
                    "pipeline. Ignored for non-create actions."
                ),
                default=None,
            ),
        ] = None,
        conversation_language: Annotated[
            str | None,
            Field(description="Conversation language, usually '*'.", default=None),
        ] = None,
        language: Annotated[
            str | None,
            Field(
                description=(
                    "Pipeline language, e.g. 'en'. For process, the language to "
                    "recognise the sentence in."
                ),
                default=None,
            ),
        ] = None,
        stt_engine: Annotated[
            str | None,
            Field(
                description="Speech-to-text engine. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        stt_language: Annotated[
            str | None,
            Field(
                description="Speech-to-text language. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        tts_engine: Annotated[
            str | None,
            Field(
                description="Text-to-speech engine. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        tts_language: Annotated[
            str | None,
            Field(
                description="Text-to-speech language. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        tts_voice: Annotated[
            str | None,
            Field(
                description="Text-to-speech voice. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        wake_word_entity: Annotated[
            str | None,
            Field(
                description="Wake-word entity ID. Pass empty string to clear.",
                default=None,
            ),
        ] = None,
        wake_word_id: Annotated[
            str | None,
            Field(
                description="Wake-word ID. Pass empty string to clear.", default=None
            ),
        ] = None,
        prefer_local_intents: Annotated[
            bool | None,
            Field(
                description=(
                    "Whether Home Assistant local intents should be preferred before "
                    "the conversation engine."
                ),
                default=None,
            ),
        ] = None,
        make_preferred: Annotated[
            bool,
            Field(
                description=(
                    "For create/update only, also set the resulting pipeline as preferred. "
                    "Ignored for other actions."
                ),
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Manage Home Assistant Assist pipelines.

        Use action='list' to discover pipeline IDs, action='get' to inspect one
        pipeline, action='create' or action='update' to write pipeline settings,
        action='set_preferred' to choose the preferred pipeline, and
        action='process' to run a sentence through Assist.

        action='process' sends the sentence straight to Assist's conversation
        agent, so a matched intent executes: it turns on the light rather than
        reporting that it would. Its result carries response_type
        ('action_done', 'query_answer' or 'error') and, on an error, error_code
        such as 'no_intent_match' — Assist declining a sentence is an answer,
        not a tool failure, so inspect those fields rather than expecting a
        raised error. Use ha_call_service to act on an entity directly; use
        this to test what Assist itself understands. When the built-in agent
        answers, a matching conversation trigger runs its automation: that
        agent checks its sentence triggers before it matches intents, so this
        is not limited to intents. Even with pipeline_id, the sentence goes
        to the agent directly. So with an agent other than the built-in one, neither
        sentence triggers nor prefer_local_intents apply — a full pipeline run
        is what adds those for other agents.

        EXAMPLES:
        - Get a pipeline (use preferred_pipeline from list for the preferred one): ha_manage_pipeline(action="get", pipeline_id="<ID from list>")
        - Create by cloning a pipeline: ha_manage_pipeline(action="create", base_pipeline_id="<ID from list>", name="Local Assist", conversation_engine="conversation.local_llm")
        - Update the agent and clear the TTS voice: ha_manage_pipeline(action="update", pipeline_id="<ID from list>", conversation_engine="conversation.local_llm", tts_voice="")
        - Run a sentence through one pipeline's agent: ha_manage_pipeline(action="process", sentence="turn on the kitchen light", pipeline_id="<ID from list>")
        - Continue a conversation: ha_manage_pipeline(action="process", sentence="and the hallway?", conversation_id="<id from the previous response>")

        Non-nullable fields such as name, language, conversation_language, and
        conversation_engine must be omitted or non-empty.
        """
        try:
            if action in {"list", "get"}:
                return await self.pipelines._manage_pipeline_read(
                    action=action,
                    pipeline_id=pipeline_id,
                )

            if action == "set_preferred":
                return await self.pipelines._set_preferred_pipeline(pipeline_id)

            if action == "process":
                return await self.pipelines._process_sentence(
                    sentence=sentence,
                    language=language,
                    conversation_id=conversation_id,
                    agent_id=agent_id,
                    pipeline_id=pipeline_id,
                )

            updates = self._pipeline_updates(
                conversation_engine=conversation_engine,
                conversation_language=conversation_language,
                language=language,
                name=name,
                stt_engine=stt_engine,
                stt_language=stt_language,
                tts_engine=tts_engine,
                tts_language=tts_language,
                tts_voice=tts_voice,
                wake_word_entity=wake_word_entity,
                wake_word_id=wake_word_id,
                prefer_local_intents=prefer_local_intents,
            )
            return await self.pipelines._write_pipeline(
                action=action,
                pipeline_id=pipeline_id,
                base_pipeline_id=base_pipeline_id,
                updates=updates,
                make_preferred=make_preferred,
            )

        except ToolFailure:
            raise
        except Exception as e:
            raise_classified(
                e,
                context={"action": action, "pipeline_id": pipeline_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Use ha_manage_pipeline(action='list') to inspect existing pipeline values",
                    "Use ha_search(domain_filter='conversation') to find conversation agent IDs",
                ],
            )
            raise
