"""Legacy error-type facade for shared config input validation."""

from ..backends.legacy import legacy_errors_sync
from ..runtime.config_write_errors import (
    reject_invalid_config_inputs as _reject_invalid_config_inputs,
)
from ..runtime.config_write_errors import (
    reject_sequence_misroute as _reject_sequence_misroute,
)

reject_invalid_config_inputs = legacy_errors_sync(_reject_invalid_config_inputs)

reject_sequence_misroute = legacy_errors_sync(_reject_sequence_misroute)
