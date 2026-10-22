"""ha_config_set_helper's ``config`` argument folded into the typed fields."""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from ...backends.legacy import legacy_errors_sync
from ...runtime.helper_params import (
    _merge_simple_helper_config as _shared_merge_simple_helper_config,
)
from ...runtime.helper_params import (
    _prepare_typed_params as _shared_prepare_typed_params,
)
from ...runtime.helper_params import (
    _simple_config_model as _shared_simple_config_model,
)
from ..component_helper_collections import fetch_helper_schemas
from .schemas import (
    _CORE_HELPER_SCHEMAS,
    SIMPLE_HELPER_TYPES,
)

_simple_config_model = legacy_errors_sync(_shared_simple_config_model)
_merge_simple_helper_config = legacy_errors_sync(_shared_merge_simple_helper_config)
_prepare_typed_params = legacy_errors_sync(_shared_prepare_typed_params)


@contextlib.asynccontextmanager
async def _core_schema_context(
    client: Any, helper_type: str, action: str | None
) -> AsyncIterator[None]:
    """Serve Core's field lists to validation errors raised inside the block."""
    token = None
    if helper_type in SIMPLE_HELPER_TYPES:
        schemas = await fetch_helper_schemas(client)
        if schemas:
            token = _CORE_HELPER_SCHEMAS.set((schemas, action or "create"))
    try:
        yield
    finally:
        if token is not None:
            _CORE_HELPER_SCHEMAS.reset(token)
