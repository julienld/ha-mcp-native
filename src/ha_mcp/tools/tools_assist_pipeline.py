"""
Assist Pipeline Tools for Home Assistant.

This module provides ha_manage_pipeline: reading, creating and updating Assist
pipelines, and running a sentence through Assist's conversation agent
(action='process'). Entity exposure to voice assistants lives in
tools_voice_assistant.py.
"""

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..errors import ErrorCode, create_error_response
from ..runtime.errors import raise_failure
from ..runtime.pipeline_tools import PipelineAction, PipelineTools
from ..runtime.pipeline_values import (
    PipelineValues,
    _drop_pipeline_id,
    process_result,
    validate_sentence,
    validate_write,
    write_result,
)
from .helpers import (
    log_tool_usage,
    register_tool_methods,
)
from .tool_hints import write_hints
from .util_helpers import websocket_error_message

logger = logging.getLogger(__name__)


class AssistPipelineTools(PipelineValues):
    """Assist pipeline management and sentence processing."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.pipelines = self

    async def _send_pipeline_message(
        self,
        message: dict[str, Any],
        *,
        operation: str,
        pipeline_id: str | None = None,
    ) -> Any:
        """Send an Assist pipeline websocket message and map HA failures."""
        result = await self._client.send_websocket_message(message)

        if not result.get("success"):
            error_msg = websocket_error_message(result.get("error", "Operation failed"))
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to {operation} Assist pipeline: {error_msg}",
                    context={"pipeline_id": pipeline_id, "operation": operation},
                )
            )

        return result.get("result")

    async def _get_pipeline(self, pipeline_id: str | None) -> dict[str, Any]:
        """Fetch one pipeline, or the preferred one when no ID is given."""
        message: dict[str, Any] = {"type": "assist_pipeline/pipeline/get"}
        if pipeline_id is not None:
            message["pipeline_id"] = pipeline_id

        pipeline = await self._send_pipeline_message(
            message,
            operation="get",
            pipeline_id=pipeline_id,
        )
        if not isinstance(pipeline, dict):
            if pipeline_id is None:
                raise_failure(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "No preferred Assist pipeline is configured",
                        context={"pipeline_id": pipeline_id, "details": pipeline},
                        suggestions=[
                            "Call ha_manage_pipeline(action='list') to find pipeline IDs.",
                            "Pass base_pipeline_id explicitly when creating a pipeline.",
                        ],
                    )
                )
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected Assist pipeline response",
                    context={"pipeline_id": pipeline_id, "details": pipeline},
                )
            )
        return pipeline

    async def _manage_pipeline_read(
        self,
        *,
        action: PipelineAction,
        pipeline_id: str | None,
    ) -> dict[str, Any]:
        """Handle Assist pipeline list/get actions."""
        if action == "list":
            data = await self._send_pipeline_message(
                {"type": "assist_pipeline/pipeline/list"},
                operation="list",
            )
            pipelines = data.get("pipelines", []) if isinstance(data, dict) else []
            return {
                "success": True,
                "operation": "list",
                "count": len(pipelines),
                "pipelines": pipelines,
                "preferred_pipeline": (
                    data.get("preferred_pipeline") if isinstance(data, dict) else None
                ),
                "message": f"Found {len(pipelines)} Assist pipeline(s)",
            }

        if pipeline_id is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "action='get' requires pipeline_id",
                    context={"action": action},
                    suggestions=[
                        "Call ha_manage_pipeline(action='list') first to find pipeline IDs."
                    ],
                )
            )

        data = await self._send_pipeline_message(
            {"type": "assist_pipeline/pipeline/get", "pipeline_id": pipeline_id},
            operation="get",
            pipeline_id=pipeline_id,
        )
        return {
            "success": True,
            "operation": "get",
            "pipeline_id": pipeline_id,
            "pipeline": data,
            "message": (
                f"Found Assist pipeline: {data.get('name', pipeline_id)}"
                if isinstance(data, dict)
                else f"Found Assist pipeline: {pipeline_id}"
            ),
        }

    async def _set_preferred_pipeline(self, pipeline_id: str | None) -> dict[str, Any]:
        """Set the preferred Assist pipeline."""
        if pipeline_id is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "action='set_preferred' requires pipeline_id",
                    context={"action": "set_preferred"},
                    suggestions=[
                        "Call ha_manage_pipeline(action='list') first to find pipeline IDs."
                    ],
                )
            )

        await self._send_pipeline_message(
            {
                "type": "assist_pipeline/pipeline/set_preferred",
                "pipeline_id": pipeline_id,
            },
            operation="set preferred",
            pipeline_id=pipeline_id,
        )
        return {
            "success": True,
            "operation": "set_preferred",
            "pipeline_id": pipeline_id,
            "message": f"Successfully set preferred Assist pipeline: {pipeline_id}",
        }

    async def _write_pipeline(
        self,
        *,
        action: PipelineAction,
        pipeline_id: str | None,
        base_pipeline_id: str | None,
        updates: dict[str, Any],
        make_preferred: bool,
    ) -> dict[str, Any]:
        """Handle Assist pipeline create/update actions."""
        validate_write(action, pipeline_id, updates, make_preferred)

        source_pipeline_id = pipeline_id if action == "update" else base_pipeline_id
        pipeline = await self._get_pipeline(source_pipeline_id)
        payload = _drop_pipeline_id(pipeline)
        payload.update(updates)

        message = {
            "type": f"assist_pipeline/pipeline/{action}",
            **payload,
        }
        if action == "update":
            message["pipeline_id"] = pipeline_id

        result_pipeline = await self._send_pipeline_message(
            message,
            operation=action,
            pipeline_id=pipeline_id,
        )
        if not isinstance(result_pipeline, dict):
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Unexpected Assist pipeline write response",
                    context={
                        "action": action,
                        "pipeline_id": pipeline_id,
                        "details": result_pipeline,
                    },
                )
            )

        result_pipeline_id = str(result_pipeline.get("id", pipeline_id))
        preferred_changed = False
        if make_preferred:
            await self._set_preferred_pipeline(result_pipeline_id)
            preferred_changed = True

        return write_result(
            action, result_pipeline, result_pipeline_id, preferred_changed
        )

    async def _process_sentence(
        self,
        *,
        sentence: str | None,
        language: str | None,
        conversation_id: str | None,
        agent_id: str | None,
        pipeline_id: str | None,
    ) -> dict[str, Any]:
        """Run a sentence through HA's conversation agent."""
        validate_sentence(sentence)

        if pipeline_id is not None:
            pipeline = await self._get_pipeline(pipeline_id)
            if agent_id is None:
                engine = pipeline.get("conversation_engine")
                agent_id = engine if isinstance(engine, str) else None
            if language is None:
                language = self._pipeline_intent_language(pipeline)

        payload: dict[str, Any] = {"text": sentence} | {
            field: value
            for field, value in (
                ("language", language),
                ("conversation_id", conversation_id),
                ("agent_id", agent_id),
            )
            if value is not None
        }

        result = await self._client._request(
            "POST", "/conversation/process", json=payload
        )
        return process_result(
            result, pipeline_id=pipeline_id, agent_id=agent_id, language=language
        )

    ha_manage_pipeline = tool(
        name="ha_manage_pipeline",
        tags={"Assist"},
        annotations=write_hints(
            "Manage Assist Pipeline",
            destructive=True,
            idempotent=False,
            open_world=True,
        ),
    )(log_tool_usage(legacy_errors(PipelineTools.ha_manage_pipeline)))


def register_assist_pipeline_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Assist pipeline tools."""
    register_tool_methods(mcp, AssistPipelineTools(client))
