"""Device payload conversion and one synchronous Core registry commit."""

from typing import Any

from homeassistant.core import HomeAssistant

from .registry_access import registry_json


def prepare_device_update(
    hass: HomeAssistant, payload: dict[str, Any]
) -> dict[str, Any]:
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import label_registry as lr

    values = dict(payload)
    if values.get("disabled_by") is not None:
        values["disabled_by"] = dr.DeviceEntryDisabler(values["disabled_by"])
    if "labels" in values:
        labels = set(values["labels"])
        values["labels"] = labels - lr.async_get_missing_label_ids(hass, labels)
    return values


def commit_device_update(
    registry: Any, device: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    from homeassistant.helpers import device_registry as dr

    callback = (
        registry.async_update_child_device
        if isinstance(device, dr.ChildDeviceEntry)
        else registry.async_update_device
    )
    written = callback(**payload)
    row = registry_json(written.dict_repr)
    if row.get("id") != payload["device_id"]:
        raise ValueError("Core returned a device with a different identity")
    return row
