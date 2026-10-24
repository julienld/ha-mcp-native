"""One public template contract, bound to a native or legacy evaluator."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION


class TemplateTools:
    def __init__(self, evaluate: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        self.evaluate = evaluate

    async def ha_eval_template(
        self,
        template: Annotated[
            str | None,
            Field(
                description="Jinja2 template to render. Omit when using 'condition'."
            ),
        ] = None,
        condition: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Automation condition to test instead of a template, e.g. "
                    "{'condition': 'numeric_state', 'entity_id': 'sensor.temp', "
                    "'above': 20}. Test several at once with {'condition': 'and', "
                    "'conditions': [...]}."
                )
            ),
        ] = None,
        variables: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Variables available to the template or condition, e.g. "
                    "sample trigger data: {'trigger': {'to_state': {'state': 'on'}}}"
                )
            ),
        ] = None,
        strict: Annotated[
            bool,
            Field(
                description=(
                    "Template only: fail on undefined variables instead of "
                    "rendering them as empty (reported in warnings when "
                    "report_errors is on)"
                )
            ),
        ] = False,
        timeout: Annotated[
            int,
            Field(
                ge=1,
                le=60,
                description="Template only: maximum render time in seconds",
            ),
        ] = 3,
        report_errors: Annotated[
            bool,
            Field(
                description=(
                    "Template only: have Home Assistant report render errors and "
                    "warnings. With false, a failed render is only written to "
                    "Home Assistant's log."
                )
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Execute a Jinja2 template render, or an automation condition check, in Home Assistant.

        Renders templates with Home Assistant's template engine (all states,
        functions and filters available), or checks a condition block the way
        an automation would and returns true/false.

        When to use: any one-shot answer DERIVED from current HA state (an
        average across sensors, a count of entities matching a condition, a
        rendered message with live values), and testing a template or condition
        before embedding it in an automation. One render beats fetching N states
        and doing the math yourself.

        When NOT to use: a plain single-entity value is ha_get_state /
        ha_search. Templates in automation `condition:` / `trigger:` positions
        or templated action names: prefer native constructs, which are validated
        at config load, where a template only errors at runtime and a non-truthy
        render is silently false — see ha_get_skill_guide for the anti-pattern
        list. Test the native condition here with `condition`.

        Results: a render that works but hits something suspect (an undefined
        variable, a missing attribute) still returns its result with the
        messages in `warnings`; strict=true makes those hard errors. A failed
        render returns Home Assistant's error text and, with the ha_mcp_tools
        component installed, the template `line` and `source_line` it failed on
        when the error names one.

        EXAMPLES:
        - ha_eval_template(template="{{ states.light | selectattr('state', 'eq', 'on') | list | count }}")
        - ha_eval_template(template="Hello {{ name }}", variables={"name": "Alice"})
        - ha_eval_template(condition={"condition": "numeric_state", "entity_id": "sensor.temperature", "above": 20})
        """
        return await self.evaluate(
            template, condition, variables, strict, timeout, report_errors
        )
