"""Device lookups across Core 2026.8 and the later child-device API."""

from typing import Any


def is_composite_device(registry: Any, device_id: str) -> bool:
    if hasattr(registry, "child_devices"):
        return (
            registry.async_get(
                device_id, include_main_devices=False, include_child_devices=False
            )
            is not None
        )
    # The old method is supported in August; September deprecates it and
    # supplies the filtered lookup above instead.
    return bool(registry.async_is_composite_device_id(device_id))


def registered_device(registry: Any, device_id: str) -> Any:
    """Read a registered main/child device, excluding synthesized composites."""
    if hasattr(registry, "child_devices"):
        return registry.async_get(device_id, include_composite_devices=False)
    if is_composite_device(registry, device_id):
        return None
    return registry.async_get(device_id)
