"""Pure public projections and errors shared by native and legacy templates."""

from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, HomeAssistantCommandError, create_error_response

from .errors import raise_failure as raise_tool_error

TEMPLATE_ERROR_SUGGESTIONS = [
    "Fix the template at the reported line, if one is given, then evaluate it again",
    "Use default values for missing data: {{ states('sensor.temp') | float(0) }}",
    "Verify entity_ids exist using ha_get_state() or ha_search()",
    "Pass values the template expects through 'variables'",
]

TEMPLATE_TIMEOUT_SUGGESTIONS = ["Simplify the template or raise 'timeout'"]

CONDITION_ERROR_SUGGESTIONS = [
    "Check the condition against the Home Assistant condition schema",
    "Verify entity_ids exist using ha_get_state() or ha_search()",
    "See ha_get_skill_guide for native condition examples",
]

# Core rejects a render that outlives its ``timeout`` with this message (code
# ``template_error``, which it also uses for other template failures).
HA_RENDER_TIMEOUT_TEXT = "Exceeded maximum execution time"


def _command_error_message(exc: HomeAssistantCommandError) -> str:
    return str(exc).removeprefix("Command failed: ")


def _render_outcome(
    error: str,
    report_errors: bool,
    result: dict[str, Any],
    diagnosis: dict[str, Any] | None,
) -> tuple[str, bool]:
    """The error to report for a failed render, and whether it timed out."""
    if not result.get("no_verdict"):
        return error, False
    # HA sent no verdict. With report_errors it always sends one, so the render
    # outran the wait; without it, silence is how HA answers a failed render.
    # The in-process render, when there is one, is the only place the actual
    # outcome is known.
    if diagnosis is not None:
        if diagnosis.get("error"):
            return diagnosis["error"], False
        if diagnosis.get("stage") == "timeout":
            return error, True
    return error, report_errors


def validate_choice(template: str | None, condition: dict[str, Any] | None) -> None:
    if (template is None) == (condition is None):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Provide exactly one of 'template' or 'condition'",
                suggestions=[
                    "Use 'template' to render a Jinja2 template",
                    "Use 'condition' to test an automation condition",
                ],
            )
        )


def render_success(template: str, result: dict[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = {
        "success": True,
        "template": template,
        "result": result.get("result"),
        "listeners": result.get("listeners", {}),
    }
    if result.get("warnings"):
        response["warnings"] = result["warnings"]
    return response


def condition_result(
    condition: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "success": True,
        "condition": condition,
        "result": result.get("result"),
    }
    # Home Assistant 2026.7+ returns template errors hit while checking a
    # template condition that still evaluated (e.g. an undefined variable).
    if result.get("template_errors"):
        data["warnings"] = list(result["template_errors"])
    return data


def raise_render_failure(
    template: str,
    report_errors: bool,
    result: dict[str, Any],
    diagnosis: dict[str, Any] | None = None,
    unavailable: str | None = None,
) -> NoReturn:
    reported = result.get("error")
    error = reported if isinstance(reported, str) else "Template evaluation failed"
    context: dict[str, Any] = {"template": template}
    if result.get("error_code"):
        context["ha_error_code"] = result["error_code"]
    if result.get("warnings"):
        context["warnings"] = result["warnings"]

    if result.get("client_error"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                error,
                context=context,
                suggestions=["Check Home Assistant WebSocket connection"],
            )
        )
    if HA_RENDER_TIMEOUT_TEXT in error:
        # A second render would only time out again.
        raise_tool_error(
            create_error_response(
                ErrorCode.TIMEOUT_OPERATION,
                error,
                context=context,
                suggestions=TEMPLATE_TIMEOUT_SUGGESTIONS,
            )
        )

    error, timed_out = _render_outcome(error, report_errors, result, diagnosis)
    if diagnosis is not None and isinstance(diagnosis.get("line"), int):
        context["line"] = diagnosis["line"]
        if diagnosis.get("source_line"):
            context["source_line"] = diagnosis["source_line"]
    elif unavailable:
        context["line_unavailable"] = unavailable

    raise_tool_error(
        create_error_response(
            ErrorCode.TIMEOUT_OPERATION if timed_out else ErrorCode.VALIDATION_FAILED,
            error,
            context=context,
            suggestions=(
                TEMPLATE_TIMEOUT_SUGGESTIONS
                if timed_out
                else TEMPLATE_ERROR_SUGGESTIONS
            ),
        )
    )


def condition_failure(
    condition: dict[str, Any], message: str, code: str | None
) -> dict[str, Any]:
    return create_error_response(
        ErrorCode.VALIDATION_FAILED,
        message,
        context={"condition": condition, "ha_error_code": code},
        suggestions=CONDITION_ERROR_SUGGESTIONS,
    )
