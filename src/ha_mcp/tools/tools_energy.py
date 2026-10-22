"""Energy preferences through native Core APIs and optional Core schema validation.

Core owns payload validation, defaults and saved state. HA-MCP owns mutation
previews, duplicate protection and optimistic locking. Semantic validation is
post-save because energy/validate only examines persisted preferences.
"""

import json
import logging
from collections.abc import Callable
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool
from ha_mcp.backends.legacy import legacy_errors, legacy_errors_sync
from ha_mcp.runtime.energy_changes import EnergyChanges
from ha_mcp.runtime.energy_tools import EnergyFacade
from ha_mcp.runtime.energy_values import (
    _append_unique_device,
    _append_unique_source,
    _check_config_hash,
    _flatten_validation_errors,
    _remove_device_by_stat,
)

from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from .core_contract import (
    command_payload,
    include_core_contract,
    validate_energy_proposal,
)
from .energy_preferences import (
    compute_per_key_hashes,
    get_energy_prefs,
    include_energy_statistics,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .tool_hints import write_hints

logger = logging.getLogger(__name__)


class EnergyTools(EnergyChanges):
    """Energy Dashboard preference management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    @property
    def energy(self) -> "EnergyTools":
        return self

    async def read(
        self, *, include_schema: bool, include_statistics: bool
    ) -> dict[str, Any]:
        result = await self._get_prefs()
        if include_schema:
            await include_core_contract(self._client, result, "energy/save_prefs")
        return (
            await include_energy_statistics(self._client, result)
            if include_statistics
            else result
        )

    async def preview(self, config: dict[str, Any]) -> dict[str, Any]:
        return await self._dry_run(config)

    async def save(
        self, config: dict[str, Any], config_hash: str | dict[str, str]
    ) -> dict[str, Any]:
        return await self._set_prefs(config, config_hash)

    async def change(self, **arguments: Any) -> dict[str, Any]:
        return await self._mutate_atomic(**arguments)

    _check_config_hash = staticmethod(legacy_errors_sync(_check_config_hash))
    _append_unique_device = staticmethod(legacy_errors_sync(_append_unique_device))
    _append_unique_source = staticmethod(legacy_errors_sync(_append_unique_source))
    _remove_device_by_stat = staticmethod(legacy_errors_sync(_remove_device_by_stat))

    ha_manage_energy_prefs = tool(
        name="ha_manage_energy_prefs",
        tags={"Energy"},
        annotations=write_hints(
            "Manage Energy Dashboard Preferences",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )(log_tool_usage(legacy_errors(EnergyFacade.ha_manage_energy_prefs)))

    async def _get_prefs(self) -> dict[str, Any]:
        return await get_energy_prefs(self._client)

    async def _dry_run(self, config: dict[str, Any]) -> dict[str, Any]:
        """Preview the actual native schema and label persisted-state checks separately."""
        validation = await validate_energy_proposal(self._client, config)
        current_errors, failure, native_validation = await self._post_save_validate()
        result: dict[str, Any] = {
            "success": True,
            "mode": "set",
            "dry_run": True,
            "proposal_validation": validation,
            "current_state_validation_errors": current_errors,
            "current_state_validation": native_validation,
            "message": "No preferences saved. Semantic checks describe the current persisted state only.",
        }
        if validation["status"] != "validated":
            result["partial"] = True
            result["warnings"] = [validation["reason"]]
        if failure:
            result["partial"] = True
            result.setdefault("warnings", []).append(
                f"Current-state validation failed: {failure}"
            )
        return result

    async def _set_prefs(
        self,
        config: dict[str, Any],
        config_hash: str | dict[str, str],
        *,
        current_prefs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Hash-check, submit to Core, then report authoritative saved state.

        Core validates every submitted entry. Existing siblings are never checked
        against a narrower local schema. The convenience path supplies its fresh
        snapshot; the per-key form locks exactly the submitted keys.
        """
        try:
            command_payload("energy/save_prefs", config)
            current_prefs = await self._resolve_current_prefs(current_prefs)

            self._check_config_hash(config, config_hash, current_prefs)

            # 3. Save
            save_payload = command_payload("energy/save_prefs", config)

            save_result = await self._client.send_websocket_message(save_payload)
            if not save_result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to save energy prefs: {save_result.get('error', 'Unknown error')}",
                        context={"mode": "set"},
                        suggestions=[
                            "Verify the token has admin privileges (energy/save_prefs is admin-only)",
                            "Check config shape against the energy/get_prefs response",
                        ],
                    )
                )

            # 4. Post-save validation against the newly-persisted state
            (
                post_save_errors,
                post_save_validate_error,
                native_validation,
            ) = await self._post_save_validate()

            # Core returns its full normalized preferences, including defaults
            # and coercions. Never predict the persisted state from the request.
            new_prefs = save_result.get("result")
            if not isinstance(new_prefs, dict):
                new_prefs = (await self._get_prefs())["config"]
            new_hash = compute_config_hash(new_prefs)

            response: dict[str, Any] = {
                "success": True,
                "mode": "set",
                "post_save_validation": native_validation,
                "config": new_prefs,
                "config_hash": new_hash,
                "config_hash_per_key": compute_per_key_hashes(new_prefs),
                "message": "Energy prefs updated.",
            }
            if post_save_errors:
                response["post_save_validation_errors"] = post_save_errors
                response.setdefault("warnings", []).append(
                    f"Save succeeded, but the persisted config has "
                    f"{len(post_save_errors)} validation error(s). Review "
                    "and re-write if any relate to this change."
                )
            elif post_save_validate_error is not None:
                response["partial"] = True
                response.setdefault("warnings", []).append(
                    f"Save succeeded, but post-save energy/validate "
                    f"failed: {post_save_validate_error}. The persisted "
                    "config has not been re-validated."
                )
            return response

        except ToolError:
            raise
        except Exception as e:
            logger.error("Error setting energy prefs", exc_info=True)
            exception_to_structured_error(
                e,
                context={"mode": "set"},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify token has admin privileges",
                    "Re-read prefs and retry with a fresh config_hash",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _resolve_current_prefs(
        self, current_prefs: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Return ``current_prefs`` if supplied, else fetch a fresh snapshot.

        Extracted from ``_set_prefs`` step 2 (snapshot acquisition).
        Convenience modes pass their already-fetched snapshot in to skip
        the re-read; external mode='set' callers fall through to a fresh
        read here. Maps "No prefs" (never configured) to the empty default
        so the hash-check works on fresh installations too.
        """
        if current_prefs is not None:
            return current_prefs

        config: dict[str, Any] = (await self._get_prefs())["config"]
        return config

    async def _post_save_validate(self) -> tuple[list[dict[str, Any]], str | None, Any]:
        """Call ``energy/validate`` after a save and return
        ``(errors, failure_message)``.

        Extracted from ``_set_prefs`` step 4 (post-save validation). A
        post-save validate failure is non-fatal — the save itself already
        succeeded — so failures are captured as a message rather than
        raised. Returns a populated (possibly empty on success) error list
        and ``None`` on a successful validate call, or an empty error list
        and a failure message when the validate call itself fails
        (transport/timeout, etc.).
        """
        post_save_errors: list[dict[str, Any]] = []
        native_validation: Any = None
        post_save_validate_error: str | None = None
        try:
            validate_result = await self._client.send_websocket_message(
                {"type": "energy/validate"}
            )
            if validate_result.get("success"):
                native_validation = validate_result.get("result")
                post_save_errors = _flatten_validation_errors(native_validation)
            else:
                post_save_validate_error = (
                    validate_result.get("error") or "unknown error"
                )
                logger.warning(
                    f"energy/validate (post-save) failed: {post_save_validate_error}"
                )
        except Exception as e:
            # Post-save validate failure is non-fatal — the save itself
            # succeeded. Log and continue.
            logger.warning("Post-save energy/validate failed", exc_info=True)
            post_save_validate_error = str(e)
        return post_save_errors, post_save_validate_error, native_validation

    # ------------------------------------------------------------------
    # Convenience modes — atomic read-modify-write (no caller hash)
    # ------------------------------------------------------------------

    # Keys overridden on the convenience-mode response envelope. Anything
    # else returned by ``_set_prefs`` (post_save_validation_errors, warning,
    # partial, plus any future additions) passes through.
    _CONVENIENCE_RESPONSE_OVERRIDES = frozenset(
        {"success", "mode", "config_hash", "target_key", "new_count", "message"}
    )

    async def _mutate_atomic_preview(
        self,
        *,
        mode: str,
        target_key: str,
        mutator: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        preview_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Preview the mutation and validate the whole resulting native payload."""
        current = await self._get_prefs()
        current_config: dict[str, Any] = current["config"]
        existing_list = list(current_config.get(target_key, []))
        new_list = mutator(existing_list)

        preview = await self._dry_run({target_key: new_list})

        return {
            **preview,
            "mode": mode,
            "dry_run": True,
            **preview_payload,
            "current_count": len(existing_list),
            "new_count": len(new_list),
        }

    @staticmethod
    def _is_hash_conflict(exc: ToolError) -> bool:
        """Return True if ``exc`` is a ``_set_prefs`` ``ToolError`` raised
        for ``RESOURCE_LOCKED`` (hash mismatch / concurrent modification).

        ``raise_tool_error`` serialises the structured error as JSON in the
        exception message, so we parse rather than substring-match.
        """
        try:
            parsed = json.loads(str(exc))
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        if not isinstance(parsed, dict):
            return False
        err_dict = parsed.get("error")
        if not isinstance(err_dict, dict):
            return False
        return bool(err_dict.get("code") == ErrorCode.RESOURCE_LOCKED.value)

    async def _mutate_atomic(
        self,
        *,
        mode: str,
        target_key: str,
        mutator: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        dry_run: bool,
        preview_payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Retry one detected snapshot conflict; Core has no conditional-save API.

        Dry runs validate the whole mutated proposal without saving. This is
        optimistic locking, not a transaction against concurrent native writers.
        """
        try:
            if dry_run:
                return await self._mutate_atomic_preview(
                    mode=mode,
                    target_key=target_key,
                    mutator=mutator,
                    preview_payload=preview_payload,
                )

            max_attempts = 2
            for attempt in range(max_attempts):
                current = await self._get_prefs()
                current_config = current["config"]
                current_hash: str = current["config_hash"]

                existing_list = list(current_config.get(target_key, []))
                new_list = mutator(existing_list)

                partial_config = {target_key: new_list}
                try:
                    set_result = await self._set_prefs(
                        partial_config,
                        current_hash,
                        current_prefs=current_config,
                    )
                except ToolError as exc:
                    # _set_prefs raises ToolError(RESOURCE_LOCKED) on hash mismatch.
                    # Retry once with a fresh read in case of a benign race.
                    if self._is_hash_conflict(exc) and attempt + 1 < max_attempts:
                        logger.warning(
                            f"{mode} on {target_key}: hash conflict on attempt "
                            f"{attempt + 1}, retrying"
                        )
                        continue
                    raise

                return {
                    "success": True,
                    "mode": mode,
                    "config_hash": set_result["config_hash"],
                    "target_key": target_key,
                    "new_count": len(new_list),
                    "message": set_result.get("message", f"{mode} succeeded."),
                    **{
                        k: v
                        for k, v in set_result.items()
                        if k not in self._CONVENIENCE_RESPONSE_OVERRIDES
                    },
                }

            # Unreachable as long as every iteration either returns or raises:
            # the only ``continue`` is gated on ``attempt + 1 < max_attempts``,
            # which is False on the final iteration — so the bare ``raise``
            # in the except block always fires there. Surface as an actionable
            # structured error rather than a bare AssertionError that would
            # otherwise fall through to ``except Exception`` and lose context.
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"_mutate_atomic({mode}, {target_key}): retry loop exited "
                    "without a return or raise",
                    context={"mode": mode, "target_key": target_key},
                    suggestions=[
                        "This indicates a bug in the optimistic-concurrency "
                        "loop logic — please file an issue with the mode and "
                        "target_key from the context.",
                    ],
                )
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error in {mode} on {target_key}: {e}")
            exception_to_structured_error(
                e,
                context={"mode": mode, "target_key": target_key},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


def register_energy_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant energy preference management tools."""
    register_tool_methods(mcp, EnergyTools(client))
