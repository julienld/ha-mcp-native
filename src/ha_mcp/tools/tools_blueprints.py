"""
Blueprint management tools for Home Assistant.

This module provides a single tool for the blueprint lifecycle: listing,
reading, importing, saving, deleting, and rendering a standalone config from an
installed automation or script blueprint. The import and save write paths
live in ``blueprint_write``; the YAML-text tier ladder in ``blueprint_sources``.
"""

import logging
from typing import Any, NamedTuple, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backends.legacy import legacy_errors
from ..client.rest_client import HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response
from ..runtime.blueprint_tools import BlueprintTools as BlueprintContractTools
from ..runtime.blueprint_values import blueprint_get_result, blueprint_list_result
from .auto_backup import with_auto_backup
from .blueprint_sources import resolve_blueprint_source
from .blueprint_substitute import substitute_blueprint
from .blueprint_write import (
    error_text,
    import_blueprint,
    normalize_blueprint_path,
    write_blueprint,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import write_hints

logger = logging.getLogger(__name__)


def blueprint_snapshot_target(kw: dict[str, Any]) -> str:
    """The blueprint path a ``save`` or ``delete`` call will act on.

    ``blueprint/save`` appends ``.yaml`` to a path without it, so the pre-write
    snapshot of a ``save`` keys on that normalised path — the file the write
    replaces. ``blueprint/delete`` takes the store key verbatim and the tool
    refuses any path that is not one, so a delete keys on the argument as given.
    """
    path = kw.get("path") or ""
    if kw.get("action") == "save" and path:
        return normalize_blueprint_path(path)
    return path


class BlueprintConsumers(NamedTuple):
    """Who uses a blueprint, and whether Home Assistant actually said.

    ``answered`` false means the lookup could not be consulted, NOT that
    nothing uses the blueprint -- the whole point of returning it is that
    an empty ``entity_ids`` must never be reported as "nothing uses this".
    """

    entity_ids: list[str]
    answered: bool


_VALID_DOMAINS = ("automation", "script")

# The reference lookup is always secondary to the action the caller asked
# for -- it enriches a refused delete, or adds ``used_by`` to a get -- so
# inheriting send_command's 30s default would make either hang far longer
# than the operation itself. Same reasoning and same leash as
# ``smart_search/_graph.py``'s ``_GRAPH_TIMEOUT_S``. Popped by the client;
# never reaches Home Assistant.
_RELATED_TIMEOUT_S = 5.0


class LegacyBlueprints:
    """Blueprint management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    _format_blueprint_list = staticmethod(blueprint_list_result)

    async def manage(
        self,
        action: str,
        domain: str = "automation",
        path: str | None = None,
        url: str | None = None,
        yaml: str | None = None,
        source_url: str | None = None,
        overwrite: bool = False,
        input: dict[str, Any] | None = None,  # noqa: A002 - public Core key
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Retain the old REST/WebSocket implementation for legacy installations."""
        try:
            # import takes its domain from the blueprint file, so a stray
            # value must not refuse an otherwise valid call.
            if action != "import":
                self._validate_domain(domain)

            if action == "list":
                return await self._list_blueprints(domain)
            if action == "get":
                return await self._get_blueprint(
                    domain, self._require(path, "path", action)
                )
            if action == "import":
                return await import_blueprint(
                    self._client, self._require(url, "url", action), overwrite
                )
            if action == "save":
                return await write_blueprint(
                    self._client,
                    domain,
                    self._require(path, "path", action),
                    self._require(yaml, "yaml", action),
                    source_url,
                    overwrite,
                )
            if action == "delete":
                return await self._delete_blueprint(
                    domain, self._require(path, "path", action), confirm
                )
            return await self._substitute_blueprint(
                domain, self._require(path, "path", action), input or {}
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"action": action, "path": path, "domain": domain, "url": url},
                suggestions=[
                    'Use ha_manage_blueprints(action="list") to see available blueprints',
                    "Verify the blueprint path or import URL is correct",
                    "Check Home Assistant connection",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    # --- Shared validation / lookups --------------------------------------

    @staticmethod
    def _validate_domain(domain: str) -> None:
        """Reject a domain Home Assistant has no blueprint store for."""
        if domain not in _VALID_DOMAINS:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid domain '{domain}'. Must be one of: {', '.join(_VALID_DOMAINS)}",
                    context={"domain": domain, "valid_domains": list(_VALID_DOMAINS)},
                )
            )

    @staticmethod
    def _require(value: str | None, name: str, action: str) -> str:
        """Return a required parameter, or raise VALIDATION_MISSING_PARAMETER."""
        if value:
            return value
        return raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                f"'{name}' is required for action='{action}'.",
                context={"action": action, "missing_parameter": name},
                suggestions=[
                    f"Pass {name} to ha_manage_blueprints(action='{action}', ...)",
                    'Use ha_manage_blueprints(action="list") to see installed blueprints',
                ],
            )
        )

    async def _fetch_blueprints(self, domain: str) -> dict[str, Any]:
        """Return the installed blueprint map for ``domain`` via blueprint/list."""
        response = await self._client.send_websocket_message(
            {"type": "blueprint/list", "domain": domain}
        )
        if not response.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    error_text(response, "Failed to query blueprints"),
                    context={"domain": domain},
                )
            )
        return response.get("result", {}) or {}

    @staticmethod
    def _raise_not_found(
        domain: str, path: str, blueprints_data: dict[str, Any]
    ) -> NoReturn:
        """Raise RESOURCE_NOT_FOUND naming a sample of the installed paths."""
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Blueprint not found: {path}",
                context={
                    "path": path,
                    "domain": domain,
                    "available_blueprints": list(blueprints_data.keys())[:10],
                },
                suggestions=[
                    (
                        f'Use ha_manage_blueprints(action="list", domain="{domain}") '
                        "to see all available blueprints"
                    ),
                    "Check the path format (e.g., 'homeassistant/motion_light.yaml')",
                ],
            )
        )

    # --- Action handlers ---------------------------------------------------

    async def _list_blueprints(self, domain: str) -> dict[str, Any]:
        """List every installed blueprint in ``domain``."""
        return self._format_blueprint_list(await self._fetch_blueprints(domain), domain)

    async def _require_installed(self, domain: str, path: str) -> dict[str, Any]:
        """Return the store, having established that ``path`` is in it.

        ``get`` and ``delete`` both have to answer "no such blueprint" before
        doing anything else; the listing is returned for ``get``, which goes on
        to read the entry out of it.
        """
        blueprints_data = await self._fetch_blueprints(domain)
        if path not in blueprints_data:
            self._raise_not_found(domain, path, blueprints_data)
        return blueprints_data

    async def _get_blueprint(self, domain: str, path: str) -> dict[str, Any]:
        """Return one blueprint's metadata, inputs, parsed body, YAML and consumers.

        ``used_by`` answers the UI's "Show automations using this blueprint"
        without having to attempt a delete and read the refusal. It comes from
        the same ``search/related`` lookup that refusal uses, so the two cannot
        disagree; when Home Assistant does not answer it, the key is omitted
        and a warning says so rather than letting an absent key read as "no
        automation uses this".
        """
        blueprints_data = await self._require_installed(domain, path)

        result, source_url = blueprint_get_result(domain, path, blueprints_data[path])
        data = result["data"]

        # Core's blueprint/list returns metadata only (never a body or the file
        # text), so both come from the tier ladder in ``blueprint_sources``.
        # Merged additively; on an install where no tier can serve the file the
        # response stays metadata + inputs.
        await self._merge_blueprint_body(result, data, domain, path, source_url)

        consumers = await self._blueprint_consumers(domain, path)
        if consumers.answered:
            data["used_by"] = consumers.entity_ids
        else:
            result.setdefault("warnings", []).append(
                "Home Assistant did not answer the reference lookup, so the "
                f"{domain}s using this blueprint are unknown — 'used_by' is "
                "omitted rather than reported as empty."
            )
        return result

    async def _delete_blueprint(
        self, domain: str, path: str, confirm: bool
    ) -> dict[str, Any]:
        """Delete an installed blueprint after confirmation and existence checks."""
        if not confirm:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Deletion not confirmed. Set confirm=True to delete blueprint '{path}'.",
                    context={"domain": domain, "path": path},
                    suggestions=[
                        (
                            f'Re-run with confirm=True: ha_manage_blueprints(action="delete", '
                            f'domain="{domain}", path="{path}", confirm=True)'
                        ),
                        f'Inspect it first: ha_manage_blueprints(action="get", domain="{domain}", path="{path}")',
                    ],
                )
            )

        await self._require_installed(domain, path)

        response = await self._client.send_websocket_message(
            {"type": "blueprint/delete", "domain": domain, "path": path}
        )
        if not response.get("success"):
            await self._raise_delete_failure(domain, path, response)

        # State verification: core answers blueprint/delete with an empty
        # result, so re-read the store rather than trusting the ack.
        if path in await self._fetch_blueprints(domain):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Home Assistant reported the delete succeeded, but blueprint "
                    f"'{path}' is still installed.",
                    context={"domain": domain, "path": path},
                    suggestions=[
                        "Check the Home Assistant logs for a filesystem error",
                        "Verify the blueprints directory is writable",
                    ],
                )
            )

        return {
            "success": True,
            "data": {"domain": domain, "path": path, "message": "Blueprint deleted."},
        }

    async def _raise_delete_failure(
        self, domain: str, path: str, response: dict[str, Any]
    ) -> NoReturn:
        """Map a failed blueprint/delete onto a structured error.

        Core raises ``BlueprintInUse`` ("Blueprint in use") while any
        automation/script still references the blueprint. That is a lock, not
        a service fault, so it becomes ``RESOURCE_LOCKED`` carrying the
        consumers the caller has to clear first.
        """
        message = error_text(response, "Failed to delete blueprint")
        if "blueprint in use" not in message.lower():
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    message,
                    context={"domain": domain, "path": path},
                    suggestions=[
                        "Check the Home Assistant logs for the underlying error",
                        f'Verify the blueprint exists: ha_manage_blueprints(action="list", domain="{domain}")',
                    ],
                )
            )

        consumers = await self._blueprint_consumers(domain, path)
        remove_tool = f"ha_config_remove_{domain}"
        set_tool = f"ha_config_set_{domain}"
        # The two set tools key their target differently; name the real
        # parameter so the call shape shown is one that validates.
        id_param = "identifier" if domain == "automation" else "script_id"
        suggestions = [
            (
                f"Delete the {domain}s that use it with {remove_tool}, or re-point "
                f"them at another blueprint with {set_tool}"
            ),
            (
                f"Removing the {domain}s is what frees the blueprint. Taking "
                f"control of them ({set_tool}({id_param}=<{domain} id>, "
                "take_control_of_blueprint=True)) "
                "converts them to standalone configs but does NOT release the "
                f"blueprint: Home Assistant keeps counting a converted {domain} "
                "as a user until it is removed"
            ),
            (
                f'Then retry: ha_manage_blueprints(action="delete", domain="{domain}", '
                f'path="{path}", confirm=True)'
            ),
        ]
        if consumers.answered:
            detail = (
                f"in use by {', '.join(consumers.entity_ids)}"
                if consumers.entity_ids
                else "reported as in use by Home Assistant"
            )
        else:
            detail = "reported as in use by Home Assistant"
            suggestions.insert(
                0,
                f"Home Assistant did not answer the reference lookup — use "
                f'ha_search(query="{path}") to find the {domain}s using it',
            )
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_LOCKED,
                f"Blueprint '{path}' cannot be deleted: {detail}.",
                context={
                    "domain": domain,
                    "path": path,
                    "in_use_by": consumers.entity_ids,
                },
                suggestions=suggestions,
            )
        )

    async def _blueprint_consumers(self, domain: str, path: str) -> BlueprintConsumers:
        """Ask HA's reference graph which entities use ``path``.

        Only the bucket named by the blueprint's own domain is read. Core's
        ``_async_search_automation_blueprint`` / ``_async_search_script_blueprint``
        fill just that one today, but the result is an open dict: were core to
        start returning related entities, devices or areas, unioning every
        bucket would list unrelated ids as blueprint consumers.

        ``answered`` is False when the ``search/related`` lookup itself could
        not be consulted, so the caller can say "unknown consumers" instead of
        "no consumers".
        """
        try:
            response = await self._client.send_websocket_message(
                {
                    "type": "search/related",
                    "item_type": f"{domain}_blueprint",
                    "item_id": path,
                    "_wait_timeout": _RELATED_TIMEOUT_S,
                }
            )
        except HomeAssistantConnectionError as exc:
            # The only class that escapes ``send_websocket_message``: it turns
            # every no-answer failure into this one and returns HA's own
            # rejections as a ``success: False`` envelope, handled below. A
            # wider catch here could only swallow a bug in this module and
            # report it as "consumers unknown".
            logger.debug("search/related failed for blueprint %s: %r", path, exc)
            return BlueprintConsumers([], False)
        if not isinstance(response, dict) or not response.get("success"):
            logger.debug("search/related rejected for blueprint %s: %r", path, response)
            return BlueprintConsumers([], False)
        result = response.get("result") or {}
        if not isinstance(result, dict):
            return BlueprintConsumers([], False)
        consumers = result.get(domain)
        if not isinstance(consumers, list):
            return BlueprintConsumers([], True)
        return BlueprintConsumers(
            sorted({item for item in consumers if isinstance(item, str)}), True
        )

    async def _substitute_blueprint(
        self, domain: str, path: str, blueprint_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Render a blueprint plus inputs into a standalone config.

        The same rendering ``ha_config_set_automation(
        take_control_of_blueprint=True)`` performs, minus the write: this
        returns the config for inspection and leaves the automation alone.
        """
        config = await substitute_blueprint(self._client, domain, path, blueprint_input)
        return {
            "success": True,
            "data": {"domain": domain, "path": path, "config": config},
        }

    async def _merge_blueprint_body(
        self,
        result: dict[str, Any],
        data: dict[str, Any],
        domain: str,
        path: str,
        source_url: str | None,
    ) -> None:
        """Merge the blueprint's body and raw YAML into the response.

        Adds ``config`` (the parsed body) to ``data`` whenever any tier produced
        one, and ``yaml`` + ``yaml_source`` whenever one produced the text —
        never a null key for a tier that found nothing. Warnings go on
        ``result`` instead: the style guide keeps ``warnings`` a top-level
        ``list[str]``, never nested in ``data``. A ``source_url`` answer earns
        its own: that text is a fresh download from the author, not the file on
        disk, so anything changed upstream since the import is in it. The
        component's own "present but could not read the body" warning is
        preserved.
        """
        found = await resolve_blueprint_source(
            self._client, domain, path, source_url=source_url
        )
        if found.config is not None:
            data["config"] = found.config
        if found.text is not None and found.source is not None:
            data["yaml"] = found.text
            data["yaml_source"] = found.source
        warnings = [w for w in (found.warning,) if w]
        if found.source == "source_url":
            warnings.append(
                f"The YAML was re-fetched from {source_url}, not read from the "
                "installed file — it includes any upstream change made since "
                "the blueprint was imported."
            )
        if warnings:
            result.setdefault("warnings", []).extend(warnings)


class BlueprintTools(BlueprintContractTools, LegacyBlueprints):
    def __init__(self, client: Any) -> None:
        LegacyBlueprints.__init__(self, client)
        BlueprintContractTools.__init__(self, self)

    # Imports learn their target after fetching; their legacy write helper captures
    # there. Saves/deletes retain their original installed-file capture policy.
    ha_manage_blueprints = tool(
        name="ha_manage_blueprints",
        tags={"Blueprints"},
        annotations=write_hints(
            "Manage Blueprints", destructive=True, idempotent=False, open_world=True
        ),
    )(
        with_auto_backup(
            domain_fn=lambda kw: f"blueprint_{kw.get('domain') or 'automation'}",
            id_fn=blueprint_snapshot_target,
            skip_fn=lambda kw: (
                kw.get("action") not in ("delete", "save")
                or (kw.get("action") == "delete" and not kw.get("confirm"))
                or (kw.get("action") == "save" and not kw.get("overwrite"))
            ),
        )(log_tool_usage(legacy_errors(BlueprintContractTools.ha_manage_blueprints)))
    )


def register_blueprint_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant blueprint management tools."""
    register_tool_methods(mcp, BlueprintTools(client))
