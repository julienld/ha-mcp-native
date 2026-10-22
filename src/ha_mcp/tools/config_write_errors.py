"""Legacy error-type facade for shared config input validation."""

from ..backends.legacy import legacy_errors_sync
from ..runtime.config_write_errors import (
    ENABLED_MISPLACED_GUIDANCE as ENABLED_MISPLACED_GUIDANCE,
)
from ..runtime.config_write_errors import (
    ENABLED_REMOVE_SUGGESTION as ENABLED_REMOVE_SUGGESTION,
)
from ..runtime.config_write_errors import (
    ENABLED_UNCHANGED_SUGGESTION as ENABLED_UNCHANGED_SUGGESTION,
)
from ..runtime.config_write_errors import (
    _missing_field_guidance as _missing_field_guidance,
)
from ..runtime.config_write_errors import _missing_fields_shape as _missing_fields_shape
from ..runtime.config_write_errors import config_has_enabled as config_has_enabled
from ..runtime.config_write_errors import (
    reject_invalid_config_inputs as _reject_invalid_config_inputs,
)
from ..runtime.config_write_errors import (
    reject_sequence_misroute as _reject_sequence_misroute,
)

reject_invalid_config_inputs = legacy_errors_sync(_reject_invalid_config_inputs)

reject_sequence_misroute = legacy_errors_sync(_reject_sequence_misroute)
