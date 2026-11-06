"""Public action metadata shared by native and legacy radio owners."""

from .radio_values import ActionSpec

ZWAVE: dict[str, ActionSpec] = {
    "diagnostics": ActionSpec(
        "Per-node Z-Wave status: node_id, status, routing, security class, "
        "Z-Wave Plus version, controller flag.",
        required=("device_id",),
    ),
    "network_status": ActionSpec(
        "Z-Wave controller summary plus per-node status/security/routing "
        "(capped at 50 nodes)."
    ),
    "ping": ActionSpec(
        "Actively ping a Z-Wave node to check it is reachable.",
        required=("device_id",),
    ),
    "add": ActionSpec(
        "Include a Z-Wave node non-interactively via SmartStart provisioning "
        "(params.smart_start=True) or active inclusion with pre-shared "
        "QR/DSK credentials. Interactive S2 read-the-PIN pairing is not "
        "supported here — use the Home Assistant UI for that.",
        long_running=True,
    ),
    "remove_device": ActionSpec(
        "Remove a Z-Wave node: open an exclusion window (default) or "
        "force-remove a known-failed node (params.failed=True).",
        destructive=True,
        long_running=True,
    ),
    "reinterview": ActionSpec(
        "Re-interview a Z-Wave node to refresh its command classes and values.",
        long_running=True,
        required=("device_id",),
    ),
    "rebuild_routes": ActionSpec(
        "Rebuild mesh routes for one node (default) or the whole network "
        "(params.scope='network').",
        long_running=True,
    ),
    "set_config_param": ActionSpec(
        "Set a Z-Wave configuration parameter on a node.",
        required=("device_id", "property", "value"),
    ),
    "get_config_params": ActionSpec(
        "List cached configuration parameters and metadata, including endpoints "
        "and partial parameters, without requiring configuration entities.",
        required=("device_id",),
    ),
    "get_config_param": ActionSpec(
        "Read a cached parameter (params.property, optional endpoint/property_key). "
        "params.refresh=True requests a raw device read of a full root parameter; "
        "endpoint/partial refresh is not supported by HA's raw-read API.",
        required=("device_id", "property"),
    ),
    "firmware_update": ActionSpec(
        "Install a pending firmware update for a Z-Wave node via its update entity.",
        long_running=True,
        required=("device_id",),
    ),
    "hard_reset": ActionSpec(
        "Factory-reset the Z-Wave controller, wiping the entire network.",
        destructive=True,
        long_running=True,
    ),
}


ZIGBEE: dict[str, ActionSpec] = {
    "diagnostics": ActionSpec(
        "Per-node ZHA diagnostics: LQI, RSSI, availability, last-seen, "
        "neighbors and routes.",
        required=("device_id",),
    ),
    "network_status": ActionSpec(
        "ZHA network settings (channel, PAN id, coordinator). Native reads do not "
        "start a topology scan."
    ),
    "permit_join": ActionSpec(
        "Open the Zigbee network for joining for `duration` seconds (default "
        "60). Pass a device_id to permit via a specific router."
    ),
    "remove_device": ActionSpec(
        "Remove (leave) a Zigbee node from the network.",
        destructive=True,
        required=("device_id",),
    ),
    "reconfigure": ActionSpec(
        "Re-interview a Zigbee node and re-apply its bindings/reporting.",
        required=("device_id",),
    ),
    "group_add": ActionSpec(
        "Create a Zigbee group. Optional group_id and members "
        "([{ieee, endpoint_id}, ...]).",
        required=("group_name",),
    ),
    "group_remove": ActionSpec(
        "Delete Zigbee groups by id (group_ids: list[int]).",
        destructive=True,
        required=("group_ids",),
    ),
    "group_members_add": ActionSpec(
        "Add members ([{ieee, endpoint_id}, ...]) to a Zigbee group.",
        required=("group_id", "members"),
    ),
    "group_members_remove": ActionSpec(
        "Remove members ([{ieee, endpoint_id}, ...]) from a Zigbee group.",
        required=("group_id", "members"),
    ),
    "bind": ActionSpec(
        "Create a binding from source_ieee to target_ieee (direct device-to-"
        "device control).",
        required=("source_ieee", "target_ieee"),
    ),
    "unbind": ActionSpec(
        "Remove the binding from source_ieee to target_ieee.",
        destructive=True,
        required=("source_ieee", "target_ieee"),
    ),
    "cluster_read": ActionSpec(
        "Read a Zigbee cluster attribute (endpoint_id, cluster_id, attribute; "
        "cluster_type defaults to 'in').",
        required=("device_id", "endpoint_id", "cluster_id", "attribute"),
    ),
    "cluster_write": ActionSpec(
        "Write a Zigbee cluster attribute (endpoint_id, cluster_id, attribute, "
        "value; cluster_type defaults to 'in').",
        destructive=True,
        required=("device_id", "endpoint_id", "cluster_id", "attribute", "value"),
    ),
    "cluster_command": ActionSpec(
        "Issue a Zigbee cluster command (endpoint_id, cluster_id, command, "
        "command_type; cluster_type defaults to 'in'). Pass command arguments "
        "via params — HA deprecated 'args' in favor of 'params' (both are still "
        "accepted), so prefer params.",
        destructive=True,
        required=("device_id", "endpoint_id", "cluster_id", "command", "command_type"),
    ),
    "network_backup": ActionSpec(
        "Create a ZHA network backup (coordinator + network key material)."
    ),
    "network_restore": ActionSpec(
        "Restore a ZHA network from a backup payload.",
        destructive=True,
        required=("backup",),
    ),
    "change_channel": ActionSpec(
        "Move the ZHA network to a new Zigbee channel (new_channel: 11-26).",
        destructive=True,
        required=("new_channel",),
    ),
    "firmware_update": ActionSpec(
        "Install the available OTA firmware on a Zigbee node (via its update.* "
        "entity).",
        long_running=True,
        required=("device_id",),
    ),
}


MATTER: dict[str, ActionSpec] = {
    "diagnostics": ActionSpec(
        "Per-node Matter diagnostics: network type (wifi/thread), availability, "
        "IPs, node type, active fabrics.",
        required=("device_id",),
    ),
    "network_status": ActionSpec("Matter fabric / integration summary."),
    "ping": ActionSpec(
        "Actively ping a Matter node at its known IP addresses.",
        required=("device_id",),
    ),
    "commission": ActionSpec(
        "Commission a Matter device from a setup code (QR or manual pairing code).",
        long_running=True,
        required=("code",),
    ),
    "commission_on_network": ActionSpec(
        "Commission an already-networked Matter device by its PIN.",
        long_running=True,
        required=("pin",),
    ),
    "share_out": ActionSpec(
        "Open a commissioning window and return a pairing code so another "
        "controller can add this device (multi-admin).",
        required=("device_id",),
    ),
    "interview": ActionSpec(
        "Re-interview a Matter node to refresh its endpoints/clusters.",
        long_running=True,
        required=("device_id",),
    ),
    "remove_fabric": ActionSpec(
        "Remove ANOTHER controller's fabric from a Matter node (pass "
        "params.fabric_index; see active_fabrics in diagnostics). To detach the "
        "device from Home Assistant itself, delete it with ha_remove_device.",
        destructive=True,
        required=("device_id",),
    ),
    "set_thread": ActionSpec(
        "Provide Thread operational credentials used for the next commissioning.",
        required=("thread_operation_dataset",),
    ),
    "set_wifi_credentials": ActionSpec(
        "Provide WiFi credentials used for the next commissioning.",
        required=("network_name", "password"),
    ),
    "firmware_update": ActionSpec(
        "Install an available OTA firmware update for this Matter node.",
        long_running=True,
        required=("device_id",),
    ),
}


THREAD: dict[str, ActionSpec] = {
    "network_status": ActionSpec(
        "OTBR border-router status keyed by extended address (channel, active "
        "dataset TLVs, extended PAN id, border agent id)."
    ),
    "list_datasets": ActionSpec(
        "List stored Thread operational datasets (the preferred network + others)."
    ),
    "set_preferred_dataset": ActionSpec(
        "Make a stored dataset preferred in Home Assistant. Does not change the "
        "OTBR's active network; use set_network for that.",
        required=("dataset_id",),
    ),
    "delete_dataset": ActionSpec(
        "Delete a stored Thread dataset. To delete the preferred dataset, "
        "make another dataset preferred first.",
        destructive=True,
        required=("dataset_id",),
    ),
    "discover_routers": ActionSpec(
        "Discover Thread border routers via mDNS. Native returns observations "
        "from a bounded window and stops its listener; legacy exposes the "
        "underlying event-stream start.",
        long_running=True,
    ),
    "create_network": ActionSpec(
        "Form a brand-new Thread network on the OTBR. Factory-resets the border "
        "router and replaces its current dataset. Targets the only OTBR when "
        "extended_address is omitted.",
        destructive=True,
    ),
    "set_network": ActionSpec(
        "Apply a stored dataset as the OTBR's active Thread network. Replaces "
        "the current network, which can drop existing Thread devices.",
        destructive=True,
        required=("dataset_id",),
    ),
    "set_channel": ActionSpec(
        "Migrate the OTBR's Thread network to a different radio channel.",
        destructive=True,
        required=("channel",),
    ),
    "add_dataset": ActionSpec(
        "Import a Thread operational dataset (hex TLV) from a named source.",
        required=("source", "tlv"),
    ),
}


SUPPORTED = {"zwave": ZWAVE, "zigbee": ZIGBEE, "matter": MATTER, "thread": THREAD}
