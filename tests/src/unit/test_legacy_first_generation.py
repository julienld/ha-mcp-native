"""First Legacy startup reuses unchanged component bindings, never replaced code."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "scenario",
    [
        "unchanged",
        "reinstall",
        "warm_reinstall",
        "override",
        "upgrade",
        "failed_install",
    ],
)
def test_legacy_first_start_preserves_only_unchanged_library_generation(
    scenario: str, tmp_path: Path
) -> None:
    script = """
        import asyncio
        import sys
        from pathlib import Path
        from types import ModuleType
        from tests.src.unit._embedded_stubs import install
        install()
        package = ModuleType('custom_components.ha_mcp_tools')
        package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
        sys.modules[package.__name__] = package
        from custom_components.ha_mcp_tools import file_access, runtime_generation
        from custom_components.ha_mcp_tools.const import DATA_LAST_PIP_SPEC, DATA_REINSTALL_REQUESTED
        from tests.src.unit.test_embedded_server import _manager, es
        import ha_mcp

        loaded_version = '9.0.0.post998'
        ha_mcp.__version__ = loaded_version
        target_version = '9.0.0.post999' if SCENARIO == 'upgrade' else loaded_version
        installed_version = loaded_version
        old_failure = file_access.ToolFailure
        old_lock = file_access.registry_update_lock
        manager, hass, entry = _manager(Path(CONFIG))
        manager._pip_spec = f'ha-mcp=={target_version}'
        if SCENARIO == 'override':
            manager._pip_spec = 'ha-mcp @ https://example.invalid/server.whl'
            manager._pip_spec_override = manager._pip_spec
        entry.data[DATA_LAST_PIP_SPEC] = manager._pip_spec
        if SCENARIO in ('reinstall', 'warm_reinstall', 'failed_install'):
            entry.data[DATA_REINSTALL_REQUESTED] = True
        es._CACHED_IMPORT_VERSION = loaded_version if SCENARIO == 'warm_reinstall' else None
        es._installed_ha_mcp_version = lambda *_args: installed_version
        es._installed_dist_version = lambda *_args: installed_version
        es._dist_installed = lambda *_args: False
        installed = []

        async def force_install(*, reinstall=False):
            global installed_version
            def replace_files():
                installed.append(reinstall)
                if SCENARIO == 'failed_install':
                    raise RuntimeError('partial install')
            await manager._async_run_tracked_install_job(replace_files)
            installed_version = target_version
        manager._async_force_install = force_install

        async def check():
            try:
                ready = await manager._async_ensure_managed_package()
            except RuntimeError as error:
                assert SCENARIO == 'failed_install' and str(error) == 'partial install'
                # A later manager must not forget a failed same-version replacement.
                successor, _, _ = _manager(Path(CONFIG))
                successor._maybe_purge_stale_modules(installed_version)
            else:
                manager._maybe_purge_stale_modules(ready)
            from ha_mcp.tool_api import ToolFailure
            from ha_mcp.utils.registry_update_lock import registry_update_lock
            if SCENARIO == 'unchanged':
                assert not installed, 'An unchanged install must not invoke the installer'
                assert ToolFailure is old_failure, 'First Legacy startup unnecessarily replaced component classes'
                assert registry_update_lock is old_lock, 'The component and runtime must share one registry lock owner'
                runtime_generation._installed_version = lambda: loaded_version
                runtime_generation.bind_native_generation()
                assert runtime_generation.native_bound_version() == loaded_version
            else:
                assert installed, 'The explicit replacement path must remain reachable'
                assert ToolFailure is not old_failure, 'The next worker must import replacement code'
                assert registry_update_lock is not old_lock
                try:
                    runtime_generation.reserve_native_generation()
                except RuntimeError as error:
                    assert 'restart Home Assistant' in str(error)
                else:
                    raise AssertionError('Native accepted component references from replaced code')
                assert runtime_generation.native_bound_version() is None
        asyncio.run(check())
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"SCENARIO = {scenario!r}\nCONFIG = {str(tmp_path)!r}\n"
            + textwrap.dedent(script),
        ],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
