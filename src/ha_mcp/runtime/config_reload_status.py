"""Report unconfirmed ordering between configuration and runtime changes."""

from typing import Any


def note_reload_outcome(
    result: dict[str, Any], reloaded: bool | None, *, domain: str, requested: bool
) -> None:
    """Warn when a runtime change could not be ordered after a config reload.

    ``reloaded`` is the ``config_reload_waiter`` verdict: False when the
    reload was never confirmed, None when nothing could watch for it.
    """
    if not requested or reloaded:
        return
    reason = (
        f"did not confirm the {domain} reload"
        if reloaded is False
        else f"could not be watched for the {domain} reload (no WebSocket)"
    )
    result.setdefault("warnings", []).append(
        f"Home Assistant {reason}; the requested runtime change may be "
        "reverted or lost when the reload completes."
    )
