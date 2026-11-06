"""Full-backup recovery keeps warnings in the established public shape."""

from ._backup_snapshot_fakes import FakeWS, _client, _entry, _run
from .test_helper_response_shape import _assert_warnings_list_shape


async def test_full_backup_restore_keeps_client_parseable_warning_lists() -> None:
    from ha_mcp.tools.backup import restore_backup

    ws = FakeWS(details=_entry("abc"))
    result = await _run(restore_backup, ws, _client(), "abc")
    _assert_warnings_list_shape(result)
