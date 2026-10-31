"""Runtime switches must preserve the SDK classes retained by native bindings."""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize("replacement", ["none", "same_version", "upgrade"])
def test_first_native_start_rejects_component_interfaces_from_an_older_import(
    replacement: str,
) -> None:
    script = """
        import asyncio
        import importlib
        import sys
        from pathlib import Path
        from types import ModuleType, SimpleNamespace
        from tests.src.unit._embedded_stubs import install
        install()
        package = ModuleType('custom_components.ha_mcp_tools')
        package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
        sys.modules[package.__name__] = package
        from custom_components.ha_mcp_tools import file_access, embedded_server, runtime_generation
        import ha_mcp
        ha_mcp.__version__ = '9.0.0.post998'
        old_failure = file_access.ToolFailure
        old_lock = file_access.registry_update_lock
        assert runtime_generation.native_bound_version() is None
        if REPLACEMENT != 'none':
            embedded_server._purge_ha_mcp_modules()
            import ha_mcp
            from ha_mcp.tool_api import ToolFailure
            from ha_mcp.utils.registry_update_lock import registry_update_lock
            from ha_mcp.utils.owned_executor import await_owned_io
            ha_mcp.__version__ = '9.0.0.post999' if REPLACEMENT == 'upgrade' else '9.0.0.post998'
            assert old_failure is not ToolFailure
            assert old_lock is not registry_update_lock
            owner = file_access.FileAccess(SimpleNamespace(config=SimpleNamespace(config_dir='.')))
            owner.active = False
            try:
                asyncio.run(owner._authorize(None, False))
            except old_failure as error:
                assert not isinstance(error, ToolFailure), 'Real component failure still belongs to the old generation'
            else:
                raise AssertionError('Inactive file owner did not reject the call')
        runtime_generation._installed_version = lambda: ha_mcp.__version__
        from custom_components.ha_mcp_tools import native_server
        class ReachedBindings(Exception):
            pass
        original_import = importlib.import_module
        def stop_at_bindings(name, *args, **kwargs):
            if name == 'ha_mcp.runtime.catalog':
                raise ReachedBindings
            return original_import(name, *args, **kwargs)
        importlib.import_module = stop_at_bindings
        try:
            try:
                native_server._prewarm()
            except ReachedBindings:
                assert REPLACEMENT == 'none', 'Mixed component interfaces reached native binding imports'
            except RuntimeError as error:
                assert REPLACEMENT != 'none', str(error)
                assert 'restart Home Assistant' in str(error)
                assert runtime_generation.native_bound_version() is None, 'A rejected native entry must not reserve legacy package mutation'
            else:
                raise AssertionError('Prewarm did not reach either boundary')
        finally:
            importlib.import_module = original_import
        if REPLACEMENT != 'none':
            manager = embedded_server.EmbeddedServerManager.__new__(embedded_server.EmbeddedServerManager)
            manager._orphaned_thread = None
            manager._pip_spec_override = ''
            embedded_server._CACHED_IMPORT_VERSION = None
            manager._maybe_purge_stale_modules(ha_mcp.__version__)
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"REPLACEMENT = {replacement!r}\n" + textwrap.dedent(script),
        ],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("installed", ["same", "changed", "unknown"])
def test_legacy_switch_never_replaces_native_resource_generation(
    installed: str,
) -> None:
    script = """
        import importlib
        import sys
        from pathlib import Path
        from types import ModuleType
        from tests.src.unit._embedded_stubs import install
        install()
        package = ModuleType('custom_components.ha_mcp_tools')
        package.__path__ = [str(Path.cwd() / 'custom_components' / 'ha_mcp_tools')]
        sys.modules[package.__name__] = package
        import ha_mcp
        from custom_components.ha_mcp_tools import embedded_server, native_server
        from custom_components.ha_mcp_tools.native_resources import _ExecutorSkillResource
        class Prepared(Exception):
            pass
        original_import = importlib.import_module
        def stop_before_catalog(name, *args, **kwargs):
            if name == 'ha_mcp.runtime.catalog':
                raise Prepared
            return original_import(name, *args, **kwargs)
        importlib.import_module = stop_before_catalog
        try:
            try:
                native_server._prewarm()
            except Prepared:
                pass
        finally:
            importlib.import_module = original_import
        manager = embedded_server.EmbeddedServerManager.__new__(embedded_server.EmbeddedServerManager)
        manager._orphaned_thread = None
        manager._pip_spec_override = ''
        embedded_server._CACHED_IMPORT_VERSION = None
        if INSTALLED == 'same':
            manager._maybe_purge_stale_modules(ha_mcp.__version__)
        else:
            try:
                manager._maybe_purge_stale_modules('9999.0.0' if INSTALLED == 'changed' else None)
            except embedded_server.EmbeddedServerError as error:
                assert error.kind == 'start'
                assert 'restart Home Assistant' in str(error)
            else:
                raise AssertionError('Changed native library generation did not require a Core restart')
        from ha_mcp._vendor.fastmcp.resources.base import Resource
        assert issubclass(_ExecutorSkillResource, Resource), 'Native Resource identity split after legacy switch'
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            f"INSTALLED = {installed!r}\n" + textwrap.dedent(script),
        ],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("defer", [False, True])
async def test_native_bindings_refuse_installer_before_any_package_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, defer: bool
) -> None:
    from unittest.mock import AsyncMock

    from .test_embedded_server import _manager, es

    manager, _hass, _entry = _manager(tmp_path)
    manager._async_wait_for_pending_install = AsyncMock()
    monkeypatch.setattr(es, "native_bound_version", lambda: "9.0.0")
    monkeypatch.setattr(
        es, "_installed_ha_mcp_version", lambda *_args: None if defer else "9.0.0"
    )
    monkeypatch.setattr(es, "_installed_dist_version", lambda *_args: "9.0.0")
    monkeypatch.setattr(es, "_dist_installed", lambda *_args: False)
    monkeypatch.setattr(
        es, "_install_reason", lambda *_args, **_kwargs: "source changed"
    )
    mutations = []
    for method in (
        "_async_remove_distribution",
        "_async_remove_legacy_target",
        "_async_remove_replaced_source",
        "_async_force_install",
        "_async_process_requirements_fast",
    ):
        operation = AsyncMock()
        monkeypatch.setattr(manager, method, operation)
        mutations.append(operation)
    with pytest.raises(es.EmbeddedServerError, match="restart Home Assistant"):
        await manager._async_ensure_managed_package(defer_mutations=defer)
    for operation in mutations:
        operation.assert_not_awaited()


def test_native_restart_refuses_a_package_replaced_on_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ha_mcp
    from custom_components.ha_mcp_tools import runtime_generation

    loaded = ha_mcp.__version__
    monkeypatch.setattr(runtime_generation, "_NATIVE_VERSION", loaded)
    monkeypatch.setattr(runtime_generation, "_installed_version", lambda: "9999.0.0")
    with pytest.raises(RuntimeError, match="restart Home Assistant"):
        runtime_generation.bind_native_generation()
    assert runtime_generation.native_bound_version() == loaded
