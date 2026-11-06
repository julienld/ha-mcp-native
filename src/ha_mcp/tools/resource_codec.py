"""Legacy exports over the shared resource codec."""

from ..backends.legacy import legacy_errors_sync
from ..runtime.resource_values import (
    _ALLOWED_DATA_URI_PREFIXES as _ALLOWED_DATA_URI_PREFIXES,
)
from ..runtime.resource_values import _B64_URLSAFE_RE as _B64_URLSAFE_RE
from ..runtime.resource_values import _DATA_URI_HEAD as _DATA_URI_HEAD
from ..runtime.resource_values import _DATA_URI_MIME as _DATA_URI_MIME
from ..runtime.resource_values import _MAX_CONTENT_BUDGET as _MAX_CONTENT_BUDGET
from ..runtime.resource_values import _MAX_ECHOED_URL as _MAX_ECHOED_URL
from ..runtime.resource_values import _PREVIEW_LENGTH as _PREVIEW_LENGTH
from ..runtime.resource_values import LEGACY_WORKER_BASE_URL as LEGACY_WORKER_BASE_URL
from ..runtime.resource_values import MAX_CONTENT_SIZE as MAX_CONTENT_SIZE
from ..runtime.resource_values import _check_ws_error as _check_ws_error
from ..runtime.resource_values import _data_uri_for as _core_data_uri_for
from ..runtime.resource_values import _decode_data_uri as _decode_data_uri
from ..runtime.resource_values import _decode_inline_content as _decode_inline_content
from ..runtime.resource_values import (
    _decode_legacy_worker_url as _decode_legacy_worker_url,
)
from ..runtime.resource_values import _has_inline_url_shape as _has_inline_url_shape
from ..runtime.resource_values import _is_data_url as _is_data_url
from ..runtime.resource_values import _is_preview_of as _is_preview_of
from ..runtime.resource_values import _looks_like_preview as _looks_like_preview
from ..runtime.resource_values import _match_data_uri_prefix as _match_data_uri_prefix
from ..runtime.resource_values import _pad_b64 as _pad_b64
from ..runtime.resource_values import _process_resource_list as _process_resource_list
from ..runtime.resource_values import _summarize_resources as _summarize_resources
from ..runtime.resource_values import content_sha256 as content_sha256

_data_uri_for = legacy_errors_sync(_core_data_uri_for)


__all__ = [
    "LEGACY_WORKER_BASE_URL",
    "MAX_CONTENT_SIZE",
    "_ALLOWED_DATA_URI_PREFIXES",
    "_B64_URLSAFE_RE",
    "_DATA_URI_HEAD",
    "_DATA_URI_MIME",
    "_MAX_CONTENT_BUDGET",
    "_MAX_ECHOED_URL",
    "_PREVIEW_LENGTH",
    "_check_ws_error",
    "_data_uri_for",
    "_decode_data_uri",
    "_decode_inline_content",
    "_decode_legacy_worker_url",
    "_has_inline_url_shape",
    "_is_data_url",
    "_is_preview_of",
    "_looks_like_preview",
    "_match_data_uri_prefix",
    "_pad_b64",
    "_process_resource_list",
    "_summarize_resources",
    "content_sha256",
]
