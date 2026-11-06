"""Preserve the administrator-token setup on Core releases before Native."""

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from . import server_credentials
from .const import (
    BIND_HOST_LOOPBACK,
    CONF_ENTRY_TYPE,
    DATA_ADMIN_TOKEN,
    DATA_ISSUED_TOKEN_ID,
    ENTRY_TYPE_SERVER,
    MIN_EMBEDDED_HOME_ASSISTANT_VERSION,
    MIN_NATIVE_HOME_ASSISTANT_VERSION,
    OPT_BIND_HOST,
    OPT_ENABLE_WEBHOOK,
    OPT_RUNTIME_MODE,
    OPT_WEBHOOK_AUTH,
    RUNTIME_LEGACY,
    RUNTIME_NATIVE,
    SERVER_ENTRY_TITLE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)


def server_minimum(user_input: dict[str, Any] | None) -> str:
    """Keep installation admission distinct from explicit Native selection."""
    return (
        MIN_NATIVE_HOME_ASSISTANT_VERSION
        if (user_input or {}).get(OPT_RUNTIME_MODE) == RUNTIME_NATIVE
        else MIN_EMBEDDED_HOME_ASSISTANT_VERSION
    )


async def async_legacy_server(
    flow: ConfigFlow, user_input: dict[str, Any] | None
) -> ConfigFlowResult:
    """Create a Legacy entry after the caller's Core and uniqueness gates."""
    from .config_flow import (
        REMOTE_ACCESS_DISABLED,
        SETUP_ADMIN_TOKEN,
        SETUP_REMOTE_ACCESS,
        _bind_host_selector,
    )

    errors: dict[str, str] = {}
    values = user_input or {}
    if user_input is not None:
        fields = server_credentials.credential_fields(user_input, SETUP_ADMIN_TOKEN)
        try:
            token, issued = await server_credentials.async_credential_from_input(
                flow.hass, **fields
            )
        except server_credentials.CredentialRefused as err:
            errors[server_credentials.error_field(fields, SETUP_ADMIN_TOKEN)] = (
                err.reason
            )
        else:
            try:
                flow._abort_if_unique_id_configured()
            except Exception:
                if issued:
                    await server_credentials.async_release_credentials(
                        flow.hass, {DATA_ISSUED_TOKEN_ID: issued}
                    )
                raise
            remote = user_input.get(SETUP_REMOTE_ACCESS, REMOTE_ACCESS_DISABLED)
            enabled = remote != REMOTE_ACCESS_DISABLED
            data = {CONF_ENTRY_TYPE: ENTRY_TYPE_SERVER, DATA_ADMIN_TOKEN: token}
            if issued:
                data[DATA_ISSUED_TOKEN_ID] = issued
            return flow.async_create_entry(
                title=SERVER_ENTRY_TITLE,
                data=data,
                options={
                    OPT_RUNTIME_MODE: RUNTIME_LEGACY,
                    OPT_ENABLE_WEBHOOK: enabled,
                    OPT_WEBHOOK_AUTH: remote if enabled else WEBHOOK_AUTH_HA,
                    OPT_BIND_HOST: user_input.get(OPT_BIND_HOST, BIND_HOST_LOOPBACK),
                },
            )
    return flow.async_show_form(
        step_id="server",
        errors=errors,
        data_schema=vol.Schema(
            {
                **server_credentials.credential_schema(vol, SETUP_ADMIN_TOKEN, values),
                vol.Required(
                    SETUP_REMOTE_ACCESS,
                    default=values.get(SETUP_REMOTE_ACCESS, REMOTE_ACCESS_DISABLED),
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            REMOTE_ACCESS_DISABLED,
                            WEBHOOK_AUTH_HA,
                            WEBHOOK_AUTH_LEGACY,
                            WEBHOOK_AUTH_NONE,
                        ],
                        translation_key="server_remote_access",
                        mode=SelectSelectorMode.LIST,
                    )
                ),
                vol.Required(
                    OPT_BIND_HOST, default=values.get(OPT_BIND_HOST, BIND_HOST_LOOPBACK)
                ): _bind_host_selector(),
            }
        ),
    )
