"""Energy convenience requests share their validation and list mutation rules."""

from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .energy_values import (
    _append_unique_device,
    _append_unique_source,
    _remove_device_by_stat,
)
from .errors import raise_failure
from .validation import validate_identifier_not_empty


class EnergyChanges:
    change: Any
    _append_unique_device = staticmethod(_append_unique_device)
    _append_unique_source = staticmethod(_append_unique_source)
    _remove_device_by_stat = staticmethod(_remove_device_by_stat)

    async def add_device(
        self,
        *,
        stat_consumption: str | None,
        name: str | None,
        included_in_stat: str | None,
        water: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Add a device-consumption entry using a fresh snapshot.

        Reads current prefs, checks for duplicate ``stat_consumption`` in the
        target list, appends the new entry, and writes back with the freshly
        captured ``config_hash``. The bound owner controls admission and
        conflict handling; a dispatched write is never replayed here.
        """
        if stat_consumption is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'stat_consumption' is required when mode='add_device'",
                    context={"mode": "add_device"},
                    suggestions=[
                        "Pass stat_consumption='sensor.<your_device_energy>'",
                    ],
                )
            )
        # Empty/whitespace stat_consumption would write a ``{"stat_consumption": ""}``
        # entry to energy prefs storage — a phantom row keyed on an empty
        # sensor reference. Same multi-modal-destructive class as
        # ``ha_manage_app`` slug guard.
        validate_identifier_not_empty(
            stat_consumption,
            "stat_consumption",
            suggestions=[
                "Pass stat_consumption='sensor.<your_device_energy>'",
            ],
            context={"mode": "add_device"},
        )

        target_key = "device_consumption_water" if water else "device_consumption"

        new_entry: dict[str, Any] = {"stat_consumption": stat_consumption}
        if name is not None:
            new_entry["name"] = name
        if included_in_stat is not None:
            new_entry["included_in_stat"] = included_in_stat

        result: dict[str, Any] = await self.change(
            mode="add_device",
            target_key=target_key,
            mutator=lambda existing: self._append_unique_device(
                existing, new_entry, target_key
            ),
            dry_run=dry_run,
            preview_payload={"would_add": new_entry, "target_key": target_key},
        )
        return result

    async def remove_device(
        self,
        *,
        stat_consumption: str | None,
        water: bool,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Remove a device-consumption entry by ``stat_consumption``."""
        if stat_consumption is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'stat_consumption' is required when mode='remove_device'",
                    context={"mode": "remove_device"},
                    suggestions=[
                        "Pass stat_consumption='sensor.<existing_device_energy>'",
                    ],
                )
            )
        # Empty/whitespace stat_consumption would search the prefs storage for
        # an empty match (always missing) and surface as a misleading
        # "Device with stat_consumption='' not found".
        validate_identifier_not_empty(
            stat_consumption,
            "stat_consumption",
            suggestions=[
                "Pass stat_consumption='sensor.<existing_device_energy>'",
            ],
            context={"mode": "remove_device"},
        )

        target_key = "device_consumption_water" if water else "device_consumption"

        result: dict[str, Any] = await self.change(
            mode="remove_device",
            target_key=target_key,
            mutator=lambda existing: self._remove_device_by_stat(
                existing, stat_consumption, target_key
            ),
            dry_run=dry_run,
            preview_payload={
                "would_remove": {"stat_consumption": stat_consumption},
                "target_key": target_key,
            },
        )
        return result

    async def add_source(
        self,
        *,
        source: dict[str, Any] | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Append a native source payload, keeping the wrapper's duplicate guard."""
        if source is None:
            raise_failure(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "'source' is required when mode='add_source'",
                    context={"mode": "add_source"},
                    suggestions=[
                        "Pass source={'type': 'grid'|'solar'|'battery'|'gas'|'water', ...}",
                    ],
                )
            )

        result: dict[str, Any] = await self.change(
            mode="add_source",
            target_key="energy_sources",
            mutator=lambda existing: self._append_unique_source(existing, source),
            dry_run=dry_run,
            preview_payload={"would_add": source, "target_key": "energy_sources"},
        )
        return result
