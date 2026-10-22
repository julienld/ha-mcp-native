"""Native/legacy configuration presentation with unchanged persisted option keys."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from . import server_credentials
from .const import (
    OPT_ADMIN_TOKEN_REPLACEMENT,
    OPT_BIND_HOST,
    OPT_PIP_SPEC,
    OPT_RUNTIME_MODE,
    OPT_SERVER_PORT,
    OPT_SERVER_URL,
    OPT_WEBHOOK_AUTH,
    RUNTIME_LEGACY,
    RUNTIME_NATIVE,
)


def runtime_schema(schema: vol.Schema, values: Mapping[str, Any]) -> vol.Schema:
    """Keep authentication first and show network/token controls only for legacy."""
    schema = vol.Schema(
        dict(
            sorted(
                schema.schema.items(),
                key=lambda item: (
                    getattr(item[0], "schema", item[0]) != OPT_WEBHOOK_AUTH
                ),
            )
        )
    )
    if values.get(OPT_RUNTIME_MODE, RUNTIME_LEGACY) != RUNTIME_NATIVE:
        return schema
    legacy_fields = {
        OPT_SERVER_PORT,
        OPT_BIND_HOST,
        OPT_PIP_SPEC,
        OPT_SERVER_URL,
        OPT_ADMIN_TOKEN_REPLACEMENT,
        server_credentials.ADMIN_USERNAME,
        server_credentials.ADMIN_PASSWORD,
    }
    return vol.Schema(
        {
            key: value
            for key, value in schema.schema.items()
            if getattr(key, "schema", key) not in legacy_fields
        }
    )
