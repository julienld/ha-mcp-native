"""Pure normalization and response projection for Assist pipelines."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure

# HA's MATCH_ALL sentinel. A pipeline driven by an LLM accepts every language and
# stores "*" as its conversation_language, which is not a language a conversation
# agent can recognise intents in.
_MATCH_ALL_LANGUAGE = "*"

# IntentResponseErrorCode.FAILED_TO_HANDLE: HA matched the intent, ran the
# action and the action raised. Unlike no_intent_match this is a failure the
# caller should see, not Assist declining a sentence.
_FAILED_TO_HANDLE_ERROR = "failed_to_handle"

_NULLABLE_PIPELINE_FIELDS = {
    "stt_engine",
    "stt_language",
    "tts_engine",
    "tts_language",
    "tts_voice",
    "wake_word_entity",
    "wake_word_id",
}


def _normalize_pipeline_value(field: str, value: Any) -> Any:
    """Convert empty string to None for clearable pipeline fields."""
    if field in _NULLABLE_PIPELINE_FIELDS and value == "":
        return None
    return value


def _drop_pipeline_id(pipeline: dict[str, Any]) -> dict[str, Any]:
    """Remove the immutable ID while retaining fields owned by Core."""
    return {field: value for field, value in pipeline.items() if field != "id"}


class PipelineValues:
    @staticmethod
    def _pipeline_updates(
        *,
        conversation_engine: str | None,
        conversation_language: str | None,
        language: str | None,
        name: str | None,
        stt_engine: str | None,
        stt_language: str | None,
        tts_engine: str | None,
        tts_language: str | None,
        tts_voice: str | None,
        wake_word_entity: str | None,
        wake_word_id: str | None,
        prefer_local_intents: bool | None,
    ) -> dict[str, Any]:
        """Collect supplied pipeline fields into HA's pipeline storage shape."""
        values = {
            "conversation_engine": conversation_engine,
            "conversation_language": conversation_language,
            "language": language,
            "name": name,
            "stt_engine": stt_engine,
            "stt_language": stt_language,
            "tts_engine": tts_engine,
            "tts_language": tts_language,
            "tts_voice": tts_voice,
            "wake_word_entity": wake_word_entity,
            "wake_word_id": wake_word_id,
            "prefer_local_intents": prefer_local_intents,
        }
        for field, value in values.items():
            if value == "" and field not in _NULLABLE_PIPELINE_FIELDS:
                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"{field} cannot be an empty string",
                        context={"field": field},
                        suggestions=[
                            f"Omit {field} to keep the existing or cloned value.",
                            f"Pass a non-empty value for {field}.",
                        ],
                    )
                )
        return {
            field: _normalize_pipeline_value(field, value)
            for field, value in values.items()
            if value is not None
        }

    @staticmethod
    def _pipeline_intent_language(pipeline: dict[str, Any]) -> str | None:
        """Resolve the language a pipeline recognises intents in.

        Mirrors HA Core's own fallback for ``conversation_language == "*"``
        (``assist_pipeline/pipeline.py``): the STT and TTS languages come first
        because they may be more specific (``zh-CN`` rather than ``zh``).
        """
        language = pipeline.get("conversation_language")
        if isinstance(language, str) and language and language != _MATCH_ALL_LANGUAGE:
            return language
        for field in ("stt_language", "tts_language", "language"):
            value = pipeline.get(field)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _speech_text(response: dict[str, Any]) -> str | None:
        """Extract the plain-text reply from an HA conversation response."""
        speech = response.get("speech")
        if not isinstance(speech, dict):
            return None
        plain = speech.get("plain")
        if not isinstance(plain, dict):
            return None
        text = plain.get("speech")
        return text if isinstance(text, str) else None


def process_result(
    result: Any, *, pipeline_id: str | None, agent_id: str | None, language: str | None
) -> dict[str, Any]:
    response = result.get("response") if isinstance(result, dict) else None
    response_type = (
        response.get("response_type") if isinstance(response, dict) else None
    )
    data = response.get("data") if isinstance(response, dict) else None
    if (
        not isinstance(response, dict)
        or not isinstance(response_type, str)
        or not isinstance(data, dict)
    ):
        # IntentResponse.as_dict always writes both response_type and data,
        # so a body missing either is malformed rather than a variant. An
        # empty or undecodable 2xx body arrives here as {} — the client
        # normalizes a JSON decode failure that way — and carrying on would
        # report success with a null response_type and no speech.
        raise_failure(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Unexpected Assist conversation response",
                context={
                    "action": "process",
                    "pipeline_id": pipeline_id,
                    "agent_id": agent_id,
                },
                suggestions=[
                    "Check that the conversation agent is loaded and responding",
                    "Use ha_get_logs to see what /conversation/process returned",
                ],
            )
        )

    speech = PipelineValues._speech_text(response)
    failed = data.get("failed", [])
    error_code = data.get("code") if response_type == "error" else None
    process_result: dict[str, Any] = {
        "success": True,
        "operation": "process",
        "pipeline_id": pipeline_id,
        "agent_id": agent_id,
        "response_type": response_type,
        "speech": speech,
        "language": response.get("language") or language,
        "conversation_id": result.get("conversation_id"),
        "continue_conversation": result.get("continue_conversation", False),
        # Every target the intent resolved. HA dropped this key in 2026.4;
        # on newer cores the split success/failed lists below carry the same
        # target dicts.
        "targets": data.get("targets", []),
        "service_calls": {
            "success": data.get("success", []),
            "failed": failed,
        },
        "error_code": error_code,
        "message": speech or f"Assist responded with {response_type}",
    }

    # HA raises only when no target succeeded, so a partial failure keeps
    # response_type 'action_done' and the intent's ordinary speech. Without
    # a warning the caller reads "Turned on the lights" and never looks in
    # service_calls.failed.
    warnings: list[str] = []
    if failed:
        failed_targets = ", ".join(
            str(target.get("id") or target.get("name") or "unknown target")
            for target in failed
            if isinstance(target, dict)
        )
        warnings.append(
            f"{len(failed)} target(s) the intent resolved failed"
            + (f": {failed_targets}" if failed_targets else "")
            + ". See service_calls.failed."
        )
    if error_code == _FAILED_TO_HANDLE_ERROR:
        warnings.append(
            "Assist matched the intent but handling it failed. Some actions may "
            "already have occurred; inspect Home Assistant before repeating the command."
        )
    if warnings:
        process_result["warnings"] = warnings
    return process_result


def validate_write(
    action: str, pipeline_id: str | None, updates: dict[str, Any], make_preferred: bool
) -> None:
    if action == "create" and (
        updates.get("name") is None or updates.get("conversation_engine") is None
    ):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "action='create' requires name and conversation_engine",
                context={"action": action},
                suggestions=[
                    "Provide name and conversation_engine.",
                    "Use ha_manage_pipeline(action='list') to inspect current pipeline values.",
                ],
            )
        )

    if action == "update" and pipeline_id is None:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "action='update' requires pipeline_id",
                context={"action": action},
                suggestions=[
                    "Call ha_manage_pipeline(action='list') first to find pipeline IDs."
                ],
            )
        )

    if not updates and not make_preferred:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "No Assist pipeline changes requested",
                context={"action": action, "pipeline_id": pipeline_id},
                suggestions=[
                    "Pass at least one pipeline field to update.",
                    "Use action='set_preferred' to only change the preferred pipeline.",
                ],
            )
        )


def write_result(
    action: str,
    result_pipeline: dict[str, Any],
    pipeline_id: str,
    preferred_changed: bool,
) -> dict[str, Any]:
    operation = "created" if action == "create" else "updated"
    return {
        "success": True,
        "operation": operation,
        "pipeline_id": pipeline_id,
        "pipeline": result_pipeline,
        "preferred_changed": preferred_changed,
        "message": (
            f"Assist pipeline {operation}: {result_pipeline.get('name', pipeline_id)}"
        ),
    }


def validate_sentence(sentence: str | None) -> None:
    if sentence is None or not sentence.strip():
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "action='process' requires a non-empty sentence",
                context={"action": "process"},
                suggestions=[
                    "Pass the command to run, e.g. "
                    "sentence='turn on the kitchen light'."
                ],
            )
        )
