"""Shared component compatibility metadata without transport imports."""

# Minimum version of the ha_mcp_tools custom component that this ha-mcp
# release expects. Bumps in lockstep with ``manifest.json`` whenever a
# server-side behavior change requires it. Older components (no
# ``version`` in the get_caller_token response, or a version below this)
# get an actionable "update via HACS" error.
# 0.8.0: ``ha_config_set_yaml`` now depends on the component's
# ``themes/*.yaml`` yaml_path scope; a <0.8.0 component reaches the old
# handler and rejects ``themes/<name>.yaml`` with a misleading "not
# allowed" message instead of this actionable update prompt.
# 0.9.0: the file tools accept absolute HAOS sibling-volume paths (/share,
# /media, /ssl, /backup — issue #1586). A <0.9.0 component's allowlist
# normalizer rejects every absolute path, so adding a volume would silently
# do nothing; the version gate surfaces an actionable "update" prompt instead.
# 0.10.0: legacy-backup restore + whole-file YAML replace add new component
# services (``replace_file``, ``list_legacy_backups``, ``read_legacy_backup``)
# and a ``yaml_path`` arg on ``read_file``. A <0.10.0 component lacks these
# services; the gate surfaces an actionable "update" instead of a raw
# "service not found".
# 0.11.0: the confirm-flow + diff work (#1720) adds ``require_confirm`` /
# ``confirm_token`` args to ``edit_yaml_config`` and ``diff`` / ``written``
# response fields. The server always sends ``require_confirm`` (from
# ENABLE_YAML_EDIT_CONFIRM, default on); a <0.11.0 component's strict
# (PREVENT_EXTRA) schema rejects the unknown arg with a raw voluptuous
# error — the gate surfaces an actionable "update" prompt instead.
# 1.2.0: the YAML fragment read (#1788, PR #1882) needs two component
# behaviours that a component reports no differently from a missing key.
# ``list_files`` honours the configured packages folder, so ha_config_get_yaml's
# glob/discovery would otherwise come back "Path not allowed" instead of
# enumerating packages; and ``include_parsed`` on ``read_file`` is a new arg
# that an older strict (PREVENT_EXTRA) schema rejects with a raw voluptuous
# error. The gate turns both into the actionable "update" prompt. The floor is
# 1.2.0, not the 1.1.0 that was current when #1882 landed: #1882 added these
# behaviours under an *un-bumped* 1.1.0 component, so 1.1.0 splits into builds
# with and without them and cannot distinguish the two (#1946). 1.2.0 is the
# first component version cut after #1882, hence the first that guarantees both.
MIN_COMPONENT_VERSION = "1.2.0"
