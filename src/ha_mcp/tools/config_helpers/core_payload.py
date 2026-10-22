"""Compatibility facade for shared Core-owned helper payload preparation."""

from ...backends.legacy import legacy_errors_sync
from ...runtime.helper_payload import (
    _DEFAULT_RANGE_KEYS as _DEFAULT_RANGE_KEYS,
)
from ...runtime.helper_payload import (
    _RANGE_KEYS as _RANGE_KEYS,
)
from ...runtime.helper_payload import (
    _reject as _shared_reject,
)
from ...runtime.helper_payload import (
    _zone_item_icon as _shared_zone_item_icon,
)
from ...runtime.helper_payload import (
    check_core_gaps as _sharedcheck_core_gaps,
)
from ...runtime.helper_payload import (
    core_fields as _sharedcore_fields,
)
from ...runtime.helper_payload import (
    merged_update as _sharedmerged_update,
)
from ...runtime.helper_payload import (
    with_create_defaults as _sharedwith_create_defaults,
)

core_fields = legacy_errors_sync(_sharedcore_fields)
with_create_defaults = legacy_errors_sync(_sharedwith_create_defaults)
merged_update = legacy_errors_sync(_sharedmerged_update)
_zone_item_icon = legacy_errors_sync(_shared_zone_item_icon)
_reject = legacy_errors_sync(_shared_reject)
check_core_gaps = legacy_errors_sync(_sharedcheck_core_gaps)
