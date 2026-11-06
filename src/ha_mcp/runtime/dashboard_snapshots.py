"""Dashboard snapshot formats shared by native and legacy restorations."""

from typing import Any

from .snapshot_files import _DASHBOARD_KIND


def valid_dashboard_capture(config: Any) -> bool:
    """The shape a ``_DASHBOARD_KIND`` snapshot's config must have."""
    if not isinstance(config, dict) or set(config) != {"dashboard", "config"}:
        return False
    row = config["dashboard"]
    return row is None or (
        isinstance(row, dict) and isinstance(row.get("url_path"), str)
    )


def dashboard_snapshot(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """A snapshot's ``{"dashboard", "config"}`` value, and whether it is a
    legacy body-only snapshot (one written without ``_DASHBOARD_KIND``)."""
    if data.get("kind") == _DASHBOARD_KIND:
        return data["config"], False
    return {"dashboard": None, "config": data["config"]}, True


def _storage_row(
    dashboards: list[dict[str, Any]] | None, url_path: str
) -> dict[str, Any] | None:
    return next(
        (
            d
            for d in dashboards or []
            if d.get("url_path") == url_path and d.get("mode") == "storage"
        ),
        None,
    )
