"""Discover configured package directories without resolving Home Assistant secrets."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]

from .const import ALLOWED_YAML_CONFIG_FILES

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


class _PackagesDir(str):
    """Marker for the folder argument of a packages ``!include_dir_*named``."""


def _capture_packages_dir(loader: yaml.Loader, node: yaml.nodes.Node) -> _PackagesDir:
    """Construct a packages include-dir tag as its folder-name argument."""
    return _PackagesDir(str(node.value))


def _follow_include(loader: yaml.Loader, node: yaml.nodes.Node) -> Any:
    """Follow ``!include`` so a split ``homeassistant:`` / ``packages:`` block in
    another file is still seen during folder discovery (#1854 review).

    Depth-guarded and best-effort: an unreadable/unparseable target (or a
    string-stream loader with no base path) yields ``None``, like any other tag.
    Only the referenced file's structure is read; every other HA tag inside it
    is still dropped by the ignore constructor.
    """
    depth = getattr(loader, "_pkg_include_depth", 0)
    name = getattr(loader, "name", "")
    if depth >= 8 or not isinstance(name, str) or not name or name.startswith("<"):
        return None
    path = os.path.join(os.path.dirname(name), str(node.value))
    # Recorded BEFORE the open, and even when it fails: the caching layer keys
    # on these files' mtimes, so an include target that does not exist yet must
    # still be tracked (with a -1 stamp) or creating it later would not
    # invalidate.
    opened = getattr(loader, "_pkg_opened", None)
    if opened is not None:
        opened.append(path)
    try:
        with open(path, encoding="utf-8") as handle:
            sub = _PackagesDirLoader(handle)
            sub._pkg_include_depth = depth + 1
            # Same list object, so a nested include lands in one signature.
            sub._pkg_opened = opened
            try:
                return sub.get_single_data()
            finally:
                sub.dispose()
    except (OSError, yaml.YAMLError):
        return None


def _ignore_unknown_tag(
    loader: yaml.Loader, tag_suffix: str, node: yaml.nodes.Node
) -> None:
    """Drop any other HA custom tag (``!secret`` / ``!env_var`` / ...).

    Folder discovery only needs the ``homeassistant: packages:`` structure, so
    unresolved tags are irrelevant — turning them into ``None`` lets
    configuration.yaml parse structurally without resolving secrets, which is
    what keeps this from being fragile hand-parsing.
    """
    return None


class _PackagesDirLoader(yaml.SafeLoader):
    """SafeLoader that captures packages include-dir folder names, follows
    ``!include``, and ignores every other HA custom tag."""


_PackagesDirLoader.add_constructor("!include_dir_named", _capture_packages_dir)


_PackagesDirLoader.add_constructor("!include_dir_merge_named", _capture_packages_dir)


_PackagesDirLoader.add_constructor("!include", _follow_include)


_PackagesDirLoader.add_multi_constructor("!", _ignore_unknown_tag)


def _extract_package_dir_markers(data: object) -> set[str]:
    """Return the raw folder argument(s) of the packages include directive(s) in
    a parsed configuration mapping. Inline packages declare no folder."""
    if not isinstance(data, dict):
        return set()
    core = data.get("homeassistant")
    if not isinstance(core, dict):
        return set()
    packages = core.get("packages")
    markers: list[_PackagesDir] = []
    if isinstance(packages, _PackagesDir):
        markers.append(packages)
    elif isinstance(packages, dict):
        markers.extend(v for v in packages.values() if isinstance(v, _PackagesDir))
    return {str(m) for m in markers}


def _load_package_dir_markers_tracked(config_path: str) -> tuple[set[str], list[str]]:
    """``_load_package_dir_markers`` that also reports every file it read.

    The file list (configuration.yaml plus any ``!include`` followed from it) is
    what ``_package_dir_markers_cached`` keys its mtime signature on, so the
    cache invalidates no matter which of them the packages directive lives in.
    """
    opened: list[str] = [config_path]
    try:
        with open(config_path, encoding="utf-8") as handle:
            loader = _PackagesDirLoader(handle)
            loader._pkg_opened = opened
            try:
                data = loader.get_single_data()
            finally:
                loader.dispose()
    except (OSError, yaml.YAMLError):
        return set(), opened
    return _extract_package_dir_markers(data), opened


def _load_package_dir_markers(config_path: str) -> set[str]:
    """Parse configuration.yaml (following ``!include``) and return the raw
    folder argument(s) of every packages ``!include_dir_*named`` directive.

    Blocking (opens files) — call via an executor. Returns raw strings, possibly
    absolute; the caller relativizes and filters them against the config dir.
    """
    markers, _opened = _load_package_dir_markers_tracked(config_path)
    return markers


def _mtime_sig(paths: list[str]) -> tuple[tuple[str, int], ...]:
    """Stamp each path with its mtime, or -1 when it does not exist.

    The -1 is load-bearing: an ``!include`` target that is missing today and
    created tomorrow changes the signature, so the cache invalidates instead of
    serving folder-detection that predates the file.
    """
    sig: list[tuple[str, int]] = []
    for path in paths:
        try:
            sig.append((path, os.stat(path).st_mtime_ns))
        except OSError:
            sig.append((path, -1))
    return tuple(sig)


_PACKAGE_DIR_CACHE: dict[str, tuple[tuple[tuple[str, int], ...], set[str]]] = {}


def _package_dir_markers_cached(config_path: str) -> set[str]:
    """``_load_package_dir_markers``, skipping the parse when nothing changed.

    Blocking (stats files) — call via an executor. Every file operation resolves
    the packages folder, and a ``ha_config_get_yaml`` glob fires one detection
    per matched file, so this would otherwise re-parse configuration.yaml (and
    whatever it includes) N times per search. Stat-per-file is cheap; the YAML
    parse is not.
    """
    cached = _PACKAGE_DIR_CACHE.get(config_path)
    if cached is not None:
        signature, markers = cached
        if _mtime_sig([path for path, _ in signature]) == signature:
            return set(markers)
    markers, opened = _load_package_dir_markers_tracked(config_path)
    # Copy in and out: the cached set must not be mutable through a caller.
    _PACKAGE_DIR_CACHE[config_path] = (_mtime_sig(opened), set(markers))
    return markers


def _package_folder_relative_to_config(raw: str, config_dir: str) -> str | None:
    """Normalize a captured packages-folder argument to a config-relative folder
    name, or ``None`` if it escapes the config dir.

    An absolute include under the config dir (``!include_dir_named
    /config/integrations``) is expressed relative to it; an absolute path
    elsewhere, a parent escape, or the config root itself is dropped.
    """
    if raw.startswith("/") or os.path.isabs(raw):
        norm = os.path.normpath(raw)
        config_dir = os.path.normpath(config_dir)
        if norm == config_dir or norm.startswith(config_dir + os.sep):
            rel = os.path.relpath(norm, config_dir)
            return rel.replace(os.sep, "/") if rel != "." else None
        return None
    folder = os.path.normpath(raw)
    if folder and folder != "." and not folder.startswith(".."):
        return folder.replace(os.sep, "/")
    return None


async def _detect_package_dirs(hass: HomeAssistant) -> set[str]:
    """Return the config-relative folder name(s) HA loads packages from.

    Home Assistant binds packages via ``homeassistant: packages:
    !include_dir_named <folder>`` (or ``!include_dir_merge_named``), where
    ``<folder>`` is any user-chosen directory — not necessarily ``packages``
    (issue #1854). The folder is read straight from that directive in
    configuration.yaml (following ``!include`` for a split ``homeassistant:``
    section), so it is found even when the folder is still empty (the first
    package is about to be created), a nested layout resolves to the configured
    include root, and an in-config absolute include is relativized.

    Always includes the built-in ``"packages"`` default, and degrades to just
    that if configuration.yaml can't be read.
    """
    dirs = {"packages"}
    config_path = hass.config.path(ALLOWED_YAML_CONFIG_FILES[0])
    # normpath is a pure string transform (no I/O), so ASYNC240 doesn't apply.
    config_dir = os.path.normpath(hass.config.config_dir)  # noqa: ASYNC240
    markers = await hass.async_add_executor_job(
        _package_dir_markers_cached, config_path
    )
    for raw in markers:
        folder = _package_folder_relative_to_config(raw, config_dir)
        if folder:
            dirs.add(folder)
    return dirs
