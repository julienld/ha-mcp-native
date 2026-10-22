"""Script config parsing, hash checks and transformations share one policy."""

import copy
from typing import Any, cast

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.coercion import parse_json_param
from ha_mcp.utils.config_hash import compute_config_hash
from ha_mcp.utils.python_sandbox import (
    PythonSandboxError,
    format_sandbox_error,
    safe_execute,
)

from .blueprint_substitute import TakenControl, take_control_config
from .config_removals import ScriptRemovalTools
from .config_write_context import ConfigWriteContext
from .errors import raise_failure
from .validation import reject_malformed_list_fields


def _strip_empty_script_fields(config: dict[str, Any]) -> dict[str, Any]:
    """
    Strip empty sequence array from script config.

    Blueprint-based scripts should not have a sequence field since this comes
    from the blueprint itself. If an empty array is present, it overrides the
    blueprint's configuration and breaks the script.

    Args:
        config: Script configuration dict

    Returns:
        Configuration with empty sequence array removed
    """
    cleaned = config.copy()

    # Remove empty sequence array for blueprint scripts
    if "sequence" in cleaned and cleaned["sequence"] == []:
        del cleaned["sequence"]

    return cleaned


class ScriptConfigTools(ScriptRemovalTools, ConfigWriteContext):
    async def _get_script_config_internal(
        self, script_id: str
    ) -> tuple[dict[str, Any], str, str | None]:
        """Fetch script config without logging or category injection.

        Returns ``(actual_config, config_hash, resolved_id)`` where
        ``actual_config`` is the inner script body (not the REST wrapper) and
        ``resolved_id`` is the storage key the REST client resolved the input to
        (from the envelope's ``script_id``). Threading ``resolved_id`` lets the
        upsert call site skip the redundant re-resolve (issue #1813 Phase 0).
        Used internally by _fetch_and_verify_hash and ha_config_get_script.

        ``resolved_id`` is ``None`` when the envelope omits ``script_id`` — the
        write target must then be re-resolved from the caller's input rather
        than defaulting to the (unresolved) caller slug, which for a renamed
        script would target the wrong storage key. This is a structural
        invariant, not a live path: ``get_script_config`` always sets the key.

        404 responses from the REST client are mapped to a structured
        ``RESOURCE_NOT_FOUND`` ToolError via ``_fetch_script_config_envelope``.
        """
        config_result = await self._fetch_script_config_envelope(script_id)
        actual_config = config_result.get("config", config_result)
        config_hash_value = compute_config_hash(actual_config)
        resolved_id = config_result.get("script_id")
        return actual_config, config_hash_value, resolved_id

    async def _fetch_and_verify_hash(
        self, script_id: str, config_hash: str, action: str
    ) -> tuple[dict[str, Any], str | None]:
        """Fetch current script config and verify config_hash for optimistic locking.

        Returns ``(actual_config, resolved_id)`` — the inner script body and the
        storage key the REST client resolved the input to. Threading
        ``resolved_id`` lets the upsert call site skip the redundant re-resolve
        (issue #1813 Phase 0); it is ``None`` when the envelope omitted the key,
        so the upsert re-resolves rather than writing to the caller slug. Raises
        ToolError if the hash does not match (conflict).
        """
        (
            actual_config,
            current_hash,
            resolved_id,
        ) = await self._get_script_config_internal(script_id)
        if current_hash != config_hash:
            raise_failure(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Script modified since last read (conflict)",
                    suggestions=[
                        "Call ha_config_get_script() again",
                        "Use the fresh config_hash from that response",
                    ],
                    context={"action": action, "script_id": script_id},
                )
            )
        self._last_verified_script = (copy.deepcopy(actual_config), resolved_id)
        return actual_config, resolved_id

    @staticmethod
    def _validate_script_config(
        config: str | dict[str, Any],
        script_id: str,
        category: str | None,
    ) -> tuple[dict[str, Any], str | None]:
        """Parse and validate script config, returning (config_dict, effective_category).

        Parses JSON string config, validates it is a dict, checks for required
        fields (sequence or use_blueprint), extracts category, and strips empty
        blueprint fields.
        """
        # Parse JSON config if provided as string
        try:
            parsed_config = parse_json_param(config, "config")
        except ValueError as e:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_JSON,
                    f"Invalid config parameter: {e}",
                    context={
                        "script_id": script_id,
                        "provided_config_type": type(config).__name__,
                    },
                )
            )

        # Ensure config is a dict
        if parsed_config is None or not isinstance(parsed_config, dict):
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Config parameter must be a JSON object",
                    context={
                        "script_id": script_id,
                        "provided_type": type(parsed_config).__name__,
                    },
                )
            )

        config_dict = cast(dict[str, Any], parsed_config)
        reject_malformed_list_fields(
            config_dict, ("sequence",), {"script_id": script_id}
        )

        # Extract category before sending to HA REST API (which rejects unknown keys).
        # Parameter takes precedence over config dict value.
        config_category = config_dict.pop("category", None)
        effective_category = category if category is not None else config_category

        # Validate required fields based on script type
        # Blueprint scripts only need use_blueprint, regular scripts need sequence
        if "use_blueprint" in config_dict:
            # Strip empty sequence array that would override blueprint
            config_dict = _strip_empty_script_fields(config_dict)
        elif "sequence" not in config_dict:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "config must include either 'sequence' field (for regular scripts) or 'use_blueprint' field (for blueprint-based scripts)",
                    context={
                        "script_id": script_id,
                        "required_fields": ["sequence OR use_blueprint"],
                    },
                )
            )

        return config_dict, effective_category

    async def _prepare_script_transform(
        self, script_id: str, config_hash: str | None, python_transform: str
    ) -> tuple[dict[str, Any], str | None]:
        """Validate + run a script python_transform.

        Returns ``(transformed_config, resolved_id)``. Extracted verbatim from
        ``ha_config_set_script``'s python_transform branch (the pre-
        ``_check_best_practices`` head): enforces config_hash, fetches +
        hash-verifies, executes the sandboxed transform, and validates the
        result still has a sequence or blueprint. Raises a structured ToolError
        on any violation.
        """
        if config_hash is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "config_hash is required for python_transform",
                    suggestions=[
                        "Call ha_config_get_script() first",
                        "Use the config_hash from that response",
                    ],
                    context={
                        "action": "python_transform",
                        "script_id": script_id,
                    },
                )
            )

        # Fetch current config and verify hash
        actual_config, resolved_id = await self._fetch_and_verify_hash(
            script_id, config_hash, "python_transform"
        )

        # Apply Python transformation on the actual script config
        try:
            transformed_config = safe_execute(python_transform, actual_config)
        except PythonSandboxError as e:
            message, suggestions = format_sandbox_error(e, python_transform)
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    message,
                    suggestions=suggestions,
                    context={
                        "action": "python_transform",
                        "script_id": script_id,
                    },
                )
            )

        reject_malformed_list_fields(
            transformed_config,
            ("sequence",),
            {"action": "python_transform", "script_id": script_id},
            source="python_transform",
        )
        # Validate transformed config
        if (
            "sequence" not in transformed_config
            and "use_blueprint" not in transformed_config
        ):
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    "Transformed config must include either 'sequence' or 'use_blueprint'",
                    suggestions=[
                        "The transform may have removed required fields",
                        "Ensure the config still has a 'sequence' or 'use_blueprint' key",
                    ],
                    context={
                        "action": "python_transform",
                        "script_id": script_id,
                    },
                )
            )
        return transformed_config, resolved_id

    async def _take_control_config(self, script_id: str) -> TakenControl:
        """Render a blueprint script into the config that replaces it.

        The hash is of the config this read saw, so the write locks against it. Produces a
        config for the ordinary replacement path rather than
        writing it here, so the rendering goes through the same validation,
        best-practice checks and skill-content attachment as every other
        script write.
        """
        current_config, fetched_hash, _ = await self._get_script_config_internal(
            script_id
        )
        taken, blueprint_path = await take_control_config(
            self._render_blueprint, "script", script_id, current_config
        )
        return TakenControl(taken, blueprint_path, fetched_hash)
