"""Shape the installed frontend's card descriptions without transport assumptions."""

from typing import Any, NoReturn

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure as raise_tool_error
from .helper_describe import compact_field


def _flatten(schema: list[Any]) -> list[dict[str, Any]]:
    """Match ha-form storage: named nodes nest unless explicitly flattened."""
    fields: list[dict[str, Any]] = []
    for node in schema:
        if not isinstance(node, dict) or node.get("type") == "divider":
            continue
        children = node.get("schema") if isinstance(node.get("schema"), list) else None
        if children is not None and node.get("name") and not node.get("flatten"):
            fields.append({**node, "schema": _flatten(children)})
        elif children is not None:
            fields.extend(_flatten(children))
        elif node.get("name"):
            fields.append(node)
    return fields


def _raise_describe_error(result: dict[str, Any], card_type: str | None) -> NoReturn:
    """Map the component's describe error to a tool error."""
    error = result.get("error")
    if error == "describe_timeout":
        raise_tool_error(
            create_error_response(
                ErrorCode.TIMEOUT_OPERATION,
                "Describing this card took longer than Home Assistant's command budget.",
                suggestions=["Retry; a loaded index usually answers within a second"],
                context={"action": "describe", "card_type": card_type},
            )
        )
    if error == "definitions_loading":
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Home Assistant is still indexing the installed frontend's card "
                "definitions (the first describe after a Home Assistant start; a "
                "slow board can take minutes). Retry shortly.",
                suggestions=[
                    "Retry in a minute; the index is kept until the next restart"
                ],
                context={"action": "describe", "card_type": card_type},
            )
        )
    if error in ("custom_cards_loading", "custom_card_not_inspected"):
        status = result.get("custom_status", {})
        loading = error == "custom_cards_loading"
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Custom card inspection is still loading; retry shortly."
                if loading
                else "This custom card could not be inspected from dashboard resources.",
                suggestions=["Retry after resource loading finishes"]
                if loading
                else [
                    "Check the resource inspection status and type spelling",
                    "Cards loaded through extra JavaScript may work in the browser without appearing in dashboard resources",
                ],
                context={
                    "action": "describe",
                    "card_type": card_type,
                    "custom_status": status,
                },
            )
        )
    if error == "unknown_card_type":
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"'{card_type}' is not a known card type.",
                suggestions=[
                    "Use one of: " + ", ".join(result.get("card_types", [])),
                ],
                context={"action": "describe", "card_type": card_type},
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Home Assistant's card definitions are unavailable.",
            details=str(error),
            suggestions=[
                "Use ha_get_skill_guide(file='references/dashboard-cards.md') for static card guidance"
            ],
            context={"action": "describe", "card_type": card_type},
        )
    )


def _no_fields_note(card_type: str) -> str:
    if card_type.startswith("custom:"):
        return "The custom card's editor fields could not be inspected; the card may still work in the browser."
    note = "No inspectable field form was found for this card."
    if card_type in {"grid", "horizontal-stack", "vertical-stack", "conditional"}:
        note += " This card nests other cards under cards or card."
    return note


def describe_card_result(
    result: dict[str, Any], card_type: str | None
) -> dict[str, Any]:
    if result.get("success") is not True:
        _raise_describe_error(result, card_type)
    if card_type is None:
        return {
            "success": True,
            "card_types": result.get("card_types", []),
            **(
                {"custom_status": result["custom_status"]}
                if "custom_status" in result
                else {}
            ),
        }
    fields = result.get("fields")
    response: dict[str, Any] = {
        "success": True,
        "card_type": card_type,
        "name": result.get("name"),
        "description": result.get("description"),
        "fields": [compact_field(f) for f in _flatten(fields)]
        if isinstance(fields, list)
        else None,
    }
    response.update(
        {
            key: value
            for key, value in result.items()
            if key in ("field_coverage", "note")
        }
    )
    if response["fields"] is None:
        response["note"] = " ".join(
            filter(None, (response.get("note"), _no_fields_note(card_type)))
        )
    return response
