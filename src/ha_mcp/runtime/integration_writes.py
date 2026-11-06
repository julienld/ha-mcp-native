"""Shared integration create/options/reconfigure/runtime mode routing."""

import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tool_api import BackendTools, ToolFailure
from ha_mcp.tools.coercion import JSON_STRING_COERCION

from .errors import raise_classified, raise_failure
from .flows.entry import (
    create_config_entry,
    refuse_component_entry,
    update_config_entry_options,
)
from .flows.session import FlowSession
from .flows.settings import backend_flow_settings
from .integration_reconfigure import (
    ReconfigureRunner,
    reject_reconfigure_only_parameters,
)
from .integration_state import set_entry_enabled, set_log_level
from .validation import validate_identifier_not_empty

logger = logging.getLogger(__name__)


def integration_changes_state(arguments: dict[str, Any]) -> bool:
    """Only the unconfirmed reconfigure preflight is guaranteed not to start a flow."""
    return not (
        arguments.get("reconfigure") is True and arguments.get("confirm_token") is None
    )


def _reject_set_integration_mode_conflicts(
    entry_id: str | None,
    domain: str | None,
    enabled: bool | None,
    config: dict[str, Any] | None,
) -> None:
    """Reject ha_set_integration argument combinations that name two modes."""
    if domain is not None and entry_id is not None:
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Pass either 'domain' (add a new integration) or "
                "'entry_id' (modify an existing one), not both",
                context={"entry_id": entry_id, "domain": domain},
            )
        )
    if enabled is not None and (domain is not None or config is not None):
        raise_failure(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "'enabled' is mutually exclusive with 'domain' and "
                "'config' — enable/disable is a separate call",
                context={"entry_id": entry_id, "domain": domain},
            )
        )


class IntegrationWriteTools(BackendTools):
    def _entry_write_guard(self, entry_id: str) -> AbstractAsyncContextManager[Any]:
        # Native owners serialize flow lifetimes and compare the snapshot again
        # after acquiring their entry lock. Remote backup stores add their scope.
        return nullcontext()

    @asynccontextmanager
    async def _integration_session(self) -> AsyncIterator[FlowSession]:
        session = FlowSession(
            self.backend,
            self.context,
            capture_backup=True,
            source_tool="ha_set_integration",
            entry_guard=self._entry_write_guard,
        )
        async with backend_flow_settings(self.backend, self.context):
            yield session

    async def ha_set_integration(
        self,
        entry_id: Annotated[
            str | None,
            Field(
                description=("Config entry ID of an existing integration."),
                default=None,
            ),
        ] = None,
        enabled: Annotated[
            bool | None,
            Field(
                description=(
                    "True to enable, False to disable the entry. Mutually "
                    "exclusive with 'domain' and 'config'."
                ),
                default=None,
            ),
        ] = None,
        domain: Annotated[
            str | None,
            Field(
                description=(
                    "Integration domain to add (e.g. 'workday', 'local_calendar')."
                ),
                default=None,
            ),
        ] = None,
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Flow form data. "
                    "Updating an existing entry — options or reconfigure — is "
                    "a patch: a field you omit keeps its current value, and a "
                    "field set to null is cleared where the integration's "
                    "schema allows that field to be empty. "
                    "Multi-step flows consume keys per step. A field two "
                    "steps declare gets your one value both times; pass "
                    "step_values={'<step_id>': {'<field>': <value>}} to give "
                    "a step its own value, or to leave the field out of that "
                    "step; a LIST of those objects supplies one per encounter "
                    "when the flow presents a step more than once. Menu steps take "
                    "'next_step_id' — a string, or a list of successive "
                    "selections for flows that present more than one menu "
                    "(e.g. a menu revisited after each branch, ending in a "
                    "finish option). The step's data_schema is returned on "
                    "validation errors so field names can be corrected."
                ),
                default=None,
            ),
        ] = None,
        reconfigure: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Use the existing config entry's official reconfigure flow "
                    "(its connection settings) instead of its options flow. "
                    "Without confirm_token this is a read-only preflight that "
                    "returns one."
                ),
            ),
        ] = False,
        expected_device_id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Requires reconfigure=True. Device registry ID the entry "
                    "must still own, before and after the change."
                ),
            ),
        ] = None,
        expected_unique_id: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Requires reconfigure=True, AND the ha_mcp_tools custom "
                    "component: Home Assistant does not expose a config "
                    "entry's unique_id over its API. Without the component "
                    "this is rejected — anchor on expected_device_id, "
                    "expected_mac or expected_entity_ids instead."
                ),
            ),
        ] = None,
        expected_mac: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Requires reconfigure=True. MAC or IEEE the entry's device "
                    "must still report."
                ),
            ),
        ] = None,
        expected_entity_ids: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Requires reconfigure=True. Exact entity IDs that must "
                    "remain associated with the entry."
                ),
            ),
        ] = None,
        confirm_token: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Requires reconfigure=True. Any token still matching "
                    "the entry's current state and the same requested config "
                    "is accepted, so a token stays valid while nothing moves."
                ),
            ),
        ] = None,
        log_level: Annotated[
            Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "DEFAULT"] | None,
            Field(
                default=None,
                description=(
                    "Set the integration's log level, like the integration "
                    "page's Enable/Disable debug logging: pass the integration "
                    "with 'domain' (no config flow runs) or 'entry_id', and "
                    "nothing else. Lasts through the next Home Assistant "
                    "restart. DEFAULT clears the override: the integration then "
                    "logs at the inherited default level, and a level set for it "
                    "in configuration.yaml returns only after a restart."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage an integration (config entry): enable/disable, add, update options, or reconfigure.

        Modes (pick one):
        - Enable/disable: entry_id + enabled.
        - Add integration: domain (+ config) — drives the domain's config
          flow, including menus and multi-step forms.
        - Update options: entry_id + config — drives the entry's options
          flow (what the "Configure" button does in the HA UI).
        - Reconfigure: entry_id + reconfigure=True + config — connection
          settings such as host, port, credentials. Repeat with the token the
          preflight returns as confirm_token to apply.
        - Log level: log_level + domain (or entry_id) — sets how much the
          integration logs; read it back with ha_get_integration's log_level.

        WHEN NOT TO USE:
        - Helpers (template, group, utility_meter, ...): use
          ha_config_set_helper. `otp` is the user's to set up in the HA UI: its
          secret is a credential enrolled in an authenticator app; do not create it.
        - Config subentries: use
          ha_config_set_helper(helper_type='config_subentry').
        - Removing an entry: use ha_remove_helpers_integrations.
        - ha-mcp's own ha_mcp_tools entries are refused: manage them in the HA
          UI, or (server settings, developer mode on) ha_dev_manage_settings /
          ha_dev_manage_server.

        Use ha_get_integration() to find entry IDs, and
        ha_get_integration(entry_id=..., include_schema=True) to inspect the
        options fields before an update. Its supports_reconfigure field tells
        you whether an entry qualifies for reconfigure=True.

        Caveats: adding an integration runs its config flow exactly as the HA
        UI would (may pair devices, scan the network, create entities). Flows
        requiring a browser step (OAuth) or an asynchronous provider step
        error out at that step instead of completing. Reconfigure edits the
        settings a live integration connects with: a wrong host or credential
        takes it offline, and there is no automatic rollback — the returned
        rollback metadata describes repeating the official flow by hand with
        the previous values, which this tool cannot read back. The preflight
        does not validate config keys against the integration's form; wrong
        field names surface on the confirm call.

        EXAMPLES:
        - Add: ha_set_integration(domain="workday", config={"name": "Workday"})
        - Update options: ha_set_integration(entry_id="abc123", config={"scan_interval": 30})
        - Reconfigure preflight: ha_set_integration(entry_id="abc123", reconfigure=True, config={"host": "10.0.0.5"}), then repeat adding confirm_token="sha256:..."
        - Debug logging: ha_set_integration(domain="zha", log_level="DEBUG"), then log_level="DEFAULT" to stop
        """
        async with self._integration_session() as session:
            try:
                if log_level is not None:
                    reject_reconfigure_only_parameters(
                        confirm_token=confirm_token,
                        expected_device_id=expected_device_id,
                        expected_unique_id=expected_unique_id,
                        expected_mac=expected_mac,
                        expected_entity_ids=expected_entity_ids,
                    )
                    return await set_log_level(
                        session,
                        entry_id,
                        domain,
                        log_level,
                        other_modes=config is not None
                        or enabled is not None
                        or reconfigure,
                    )

                if reconfigure:
                    session.helper_type = "integration"
                    return await ReconfigureRunner(session).handle_mode(
                        entry_id=entry_id,
                        domain=domain,
                        enabled=enabled,
                        config=config,
                        expected_device_id=expected_device_id,
                        expected_unique_id=expected_unique_id,
                        expected_mac=expected_mac,
                        expected_entity_ids=expected_entity_ids,
                        confirm_token=confirm_token,
                    )

                reject_reconfigure_only_parameters(
                    confirm_token=confirm_token,
                    expected_device_id=expected_device_id,
                    expected_unique_id=expected_unique_id,
                    expected_mac=expected_mac,
                    expected_entity_ids=expected_entity_ids,
                )

                _reject_set_integration_mode_conflicts(
                    entry_id, domain, enabled, config
                )

                if domain is not None:
                    # Add mode: drive the domain's config flow.
                    validate_identifier_not_empty(
                        domain,
                        "domain",
                        suggestions=[
                            "Pass the integration domain, e.g. 'workday' or "
                            "'local_calendar'",
                        ],
                    )
                    refuse_component_entry(domain, action="add an entry for")
                    return await create_config_entry(session, domain, config or {})

                if entry_id is None:
                    raise_failure(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "Nothing to do — provide 'domain' to add an "
                            "integration, or 'entry_id' with 'enabled' "
                            "(enable/disable) or 'config' (update options)",
                            suggestions=[
                                "Use ha_get_integration() to find valid config entry IDs",
                            ],
                        )
                    )

                # Empty/whitespace entry_id would surface as a misleading HA
                # "config entry not found" from the backend call.
                entry_id = validate_identifier_not_empty(
                    entry_id,
                    "entry_id",
                    suggestions=[
                        "Use ha_get_integration() to find valid config entry IDs",
                    ],
                )

                if enabled is not None:
                    session.helper_type = "integration"
                    return await set_entry_enabled(session, entry_id, enabled)

                if config is not None:
                    # Options mode: drive the entry's options flow.
                    async with session.write_scope(entry_id):
                        result = await update_config_entry_options(
                            session, entry_id, config
                        )
                    if session.warnings:
                        result.setdefault("warnings", []).extend(session.warnings)
                    return result

                raise_failure(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Nothing to do — pass 'enabled' to enable/disable the "
                        "entry, or 'config' to update its options",
                        context={"entry_id": entry_id},
                    )
                )
                return None  # unreachable: raise_tool_error raises

            except ToolFailure:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to set integration: {e}")
                error_context = self._set_integration_error_context(entry_id, domain)
                raise_classified(
                    e,
                    context=error_context,
                    suggestions=[
                        "Verify the integration domain is spelled correctly and "
                        "is installed (custom integrations must be installed "
                        "before a config flow can start)",
                    ]
                    if domain is not None
                    else [
                        "Use ha_get_integration() to find valid config entry IDs",
                    ],
                )
                return None  # unreachable: exception_to_structured_error raises

    @staticmethod
    def _set_integration_error_context(
        entry_id: str | None, domain: str | None
    ) -> dict[str, Any]:
        """Build the error-context dict for ha_set_integration failures."""
        error_context: dict[str, Any] = {}
        if entry_id is not None:
            error_context["entry_id"] = entry_id
        if domain is not None:
            error_context["domain"] = domain
        return error_context
