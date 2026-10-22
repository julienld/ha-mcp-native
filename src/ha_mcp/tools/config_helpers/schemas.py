"""Compatibility exports for the shared helper schema and response contract."""

from typing import Any

from ...runtime.helper_schema import (
    _CORE_HELPER_SCHEMAS as _CORE_HELPER_SCHEMAS,
)
from ...runtime.helper_schema import (
    _HELPER_SKILL_FILES as _HELPER_SKILL_FILES,
)
from ...runtime.helper_schema import (
    _INITIAL_DISABLES_RESTORE_DESCRIPTION as _INITIAL_DISABLES_RESTORE_DESCRIPTION,
)
from ...runtime.helper_schema import (
    _SIMPLE_CONFIG_KEYS_DESCRIPTION as _SIMPLE_CONFIG_KEYS_DESCRIPTION,
)
from ...runtime.helper_schema import (
    SIMPLE_HELPER_SCHEMAS as SIMPLE_HELPER_SCHEMAS,
)
from ...runtime.helper_schema import (
    SIMPLE_HELPER_TYPES as SIMPLE_HELPER_TYPES,
)
from ...runtime.helper_schema import (
    HelperResponse as HelperResponse,
)
from ...runtime.helper_schema import (
    StorageHelperType as StorageHelperType,
)
from ...runtime.helper_schema import (
    _core_helper_fields as _core_helper_fields,
)
from ...runtime.helper_schema import (
    _helper_response as _helper_response,
)
from ...runtime.helper_schema import (
    _HelperFieldSpec as _HelperFieldSpec,
)
from ...runtime.helper_schema import (
    _HelperFieldSpecBase as _HelperFieldSpecBase,
)
from ...runtime.helper_schema import (
    _simple_helper_error_context as _simple_helper_error_context,
)
from ...runtime.helper_schema import (
    get_simple_helper_schema as get_simple_helper_schema,
)
from ..config_write_helpers import attach_skill_content


def _attach_helper_skill(response: dict[str, Any], MandatoryBPS: bool) -> None:
    """In-place attach skill_content to a helper response when applicable.

    Helper tool has no best-practice checker integration, so
    ``referenced_files`` is always None — embedding is driven purely by
    the ``MandatoryBPS`` flag. Delegates to the shared
    :func:`attach_skill_content` so the missing-vendor-warning path is
    consistent across every write tool.
    """
    attach_skill_content(
        response,
        MandatoryBPS=MandatoryBPS,
        canonical_files=_HELPER_SKILL_FILES,
        referenced_files=None,
    )
