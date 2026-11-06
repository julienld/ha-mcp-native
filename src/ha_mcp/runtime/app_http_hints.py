"""Route-specific app HTTP guidance shared by legacy and native transports."""

from typing import Any


def _front_door_state(addon: dict[str, Any]) -> bool | None:
    """Return the app's ``leave_front_door_open`` state, or None when unexposed.

    Supervisor's ``options`` dict omits an option the user never saved even
    when the app's schema exposes it, and the app treats that absence as
    disabled — so the schema, not key presence, decides whether the option
    applies (stock installs ship the key in the schema only).
    """
    options = addon.get("options")
    if isinstance(options, dict) and "leave_front_door_open" in options:
        return bool(options["leave_front_door_open"])
    schema = addon.get("schema")
    if isinstance(schema, list) and any(
        isinstance(item, dict) and item.get("name") == "leave_front_door_open"
        for item in schema
    ):
        return False
    return None


def _addon_config_for_http_hint(addon: dict[str, Any]) -> dict[str, Any]:
    """Return the app settings that can explain direct-access failures."""
    return {
        "options": addon.get("options"),
        "ports": addon.get("network") or addon.get("ports") or None,
        "host_network": addon.get("host_network"),
        "ingress_port": addon.get("ingress_port"),
    }


def _direct_port_auth_suggestion(slug: str) -> str:
    """Explain the configurable direct-access auth trade-off, Ingress first."""
    return (
        "The app (add-on) rejected this direct-port request. Prefer Ingress: retry "
        f"without the 'port' parameter (ha_manage_app(slug='{slug}', "
        "path='...')). The app's 'leave_front_door_open' option controls "
        "direct-access authentication; only if the user explicitly accepts "
        "the security trade-off, use "
        f"ha_manage_app(slug='{slug}', "
        "options={'leave_front_door_open': True}), then "
        f"ha_manage_app(slug='{slug}', action='restart'), and retry. Enabling "
        "it removes authentication from the app's direct-access surface for "
        "hosts that can reach the mapped port."
    )


def _direct_port_rejection_suggestion() -> str:
    """Explain a direct-port rejection the front-door option cannot fix.

    Fires when the app exposes no ``leave_front_door_open`` option, or when it
    is already enabled — either way the remedy lives in the app's own auth
    configuration, not in that option.
    """
    return (
        "The app (add-on) rejected this direct-port request. Check the app's own "
        "authentication and access-control settings, IP allowlist, and logs."
    )


def add_http_error_hints(
    result: dict[str, Any],
    status_code: int,
    addon: dict[str, Any],
    slug: str,
    direct_port: bool,
) -> None:
    """Mutate result to add an error key for 4xx/5xx responses, with tailored suggestions for 401 and 403."""
    if status_code >= 400:
        result["error"] = f"App (add-on) API returned HTTP {status_code}"
        # Prefer the caller-resolved slug (authoritative); fall back to the
        # app dictionary, then a placeholder only if neither is populated.
        slug_val = slug or addon.get("slug") or "<slug>"
        if status_code == 401:
            if direct_port:
                result["addon_config"] = _addon_config_for_http_hint(addon)
                if _front_door_state(addon) is False:
                    result["suggestion"] = _direct_port_auth_suggestion(slug_val)
                else:
                    result["suggestion"] = _direct_port_rejection_suggestion()
            else:
                # An ingress 401 is a credential/session problem. Keep network
                # configuration out of that result so the caller fixes the HA
                # token or ingress session instead of weakening app authentication.
                result["suggestion"] = (
                    "Authentication failed. The ingress session may have expired, "
                    "or your HA token may lack the required scope. Verify the "
                    "token has admin rights and try again."
                )
        elif status_code == 403:
            # A direct-port 403 is app-level access control; an ingress 403 is
            # typically an Nginx IP ACL blocking direct access — a network
            # configuration problem. Attach addon_config either way so the LLM
            # can see the port mapping.
            result["addon_config"] = _addon_config_for_http_hint(addon)
            if direct_port:
                if _front_door_state(addon) is False:
                    result["suggestion"] = _direct_port_auth_suggestion(slug_val)
                else:
                    result["suggestion"] = _direct_port_rejection_suggestion()
            else:
                ports_dict = addon.get("network") or addon.get("ports") or {}
                unmapped = sorted(k for k, v in ports_dict.items() if v is None)
                example_proto = unmapped[0] if unmapped else ""
                example_port = example_proto.split("/", 1)[0] if example_proto else ""
                if unmapped and example_port.isdigit():
                    addon_label = addon.get("name") or slug_val
                    result["suggestion"] = (
                        f"Map {example_proto} to a host port in the HA UI "
                        f"('{addon_label}' → Configuration → Network), restart the "
                        f"app, then retry with ha_manage_app(slug='{slug_val}', "
                        f"path='...', port={example_port})."
                    )
                else:
                    result["suggestion"] = (
                        "This app (add-on) is blocking direct connections (likely Nginx IP restriction). "
                        "Try using the 'port' parameter to connect to the app's direct access port "
                        "(see addon_config.ports above) with 'leave_front_door_open' enabled. "
                        "Example: ha_manage_app(slug='...', path='...', port=<direct_port>). "
                        "The user may need to change app settings in the HA UI and restart the app."
                    )
