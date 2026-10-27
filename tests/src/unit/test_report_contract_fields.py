"""The report projection example retains the instructions and prerequisite hints."""


def test_recommended_runtime_projection_keeps_both_hints():
    """The ``fields`` example an agent copies must not drop the pre-check
    fields that ``instructions`` then tells it to read."""
    import re
    from typing import get_args, get_type_hints

    from pydantic.fields import FieldInfo

    from ha_mcp.runtime.report_tools import ReportFacade

    annotation = get_type_hints(ReportFacade.ha_report_issue, include_extras=True)[
        "fields"
    ]
    description = next(
        item.description for item in get_args(annotation) if isinstance(item, FieldInfo)
    )
    match = re.search("Typical: '([^']+)'", description)
    assert match, "fields description lost its runtime-bug example"
    recommended = set(match.group(1).split(","))
    assert {
        "missing_tool_hint",
        "known_client_issues_hint",
        "instructions",
    } <= recommended
