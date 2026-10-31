# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Inventory native-migration boundaries without importing Home Assistant.

The default scan is static and dependency-free. ``--capture-runtime`` additionally
uses the repository's installed development environment to record real FastMCP
JSON Schemas; run it with ``uv run --no-project --python .venv/Scripts/python.exe``
or the platform-equivalent project interpreter. No command contacts Home Assistant.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import importlib
import importlib.util
import json
import os
import pkgutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "ha_mcp"
OUTPUT = ROOT / "docs" / "native-migration"
BASELINE = "2dc504fd97d8416f87b6ea7f2fd8ce0332615956"
NATIVE_CATALOG_SOURCES = (
    SOURCE / "runtime" / "catalog.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_label_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_operations" / "templates.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_trace_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_device_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_resource_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_history_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_log_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_entity_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_event_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_group_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_camera_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_overview_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_health_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_dashboard_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_calendar_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_bulk_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_blueprint_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_search_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_energy_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_system_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_theme_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_pipeline_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_update_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_self_update_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_radio_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_filesystem_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_yaml_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_screenshot_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_hacs_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_app_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_backup_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_dev_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_report_tools.py",
    ROOT / "custom_components" / "ha_mcp_tools" / "native_security_policy_tools.py",
)
TRANSPORT_METHODS = frozenset(
    {
        "send_websocket_message",
        "send_command",
        "_request",
        "request",
        "get",
        "post",
        "put",
        "delete",
        "get_state",
        "get_states",
        "get_services",
        "call_service",
        "get_config",
        "get_history",
        "get_logbook",
        "get_websocket_client",
        "websocket_client",
        "render_template",
        "fire_event",
        "get_error_log",
        "check_config",
        "get_events",
        "get_calendars",
        "get_calendar_events",
    }
)


def _load_script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        f"native_inventory_{name}", ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _method_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _wire_literals(node: ast.AST) -> list[str]:
    """Literal command names and API paths, including non-call dictionaries."""
    found = set()
    for item in ast.walk(node):
        if isinstance(item, ast.Dict):
            for key, value in zip(item.keys, item.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "type":
                    if (
                        isinstance(value, ast.Constant)
                        and isinstance(value.value, str)
                        and value.value
                        not in {
                            "object",
                            "array",
                            "string",
                            "number",
                            "integer",
                            "boolean",
                            "null",
                        }
                    ):
                        found.add(value.value)
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            literal = item.value
            if literal.startswith(("/api/", "ha_mcp_tools/")) and "\n" not in literal:
                found.add(literal)
    return sorted(found)


def _module_scan(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _method_name(node) in TRANSPORT_METHODS:
            receiver = ast.unparse(node.func)
            # Do not treat ordinary dict.get/list methods as client operations.
            if not any(
                part in receiver.lower()
                for part in ("client", "websocket", "session", "_request")
            ):
                continue
            calls.append(
                {
                    "line": node.lineno,
                    "call": receiver,
                    "wire_literals": _wire_literals(node),
                }
            )
    operations = {
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "execute"
        and "backend" in ast.unparse(call.func)
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    }
    return {
        "calls": calls,
        "wire_literals": _wire_literals(tree),
        "semantic_operations": sorted(operations),
    }


def _family(path: str) -> tuple[str, str, list[str]]:
    families = (
        (
            ("backup.py",),
            "backup-and-restore",
            "high",
            [
                "capture all affected resources",
                "restore owner and readback",
                "partial failure recovery",
            ],
        ),
        (
            ("assist", "voice"),
            "assist",
            "medium",
            [
                "pipeline lifecycle",
                "native exposure helpers",
                "anonymous Assist identity policy",
            ],
        ),
        (
            ("calendar", "todo"),
            "collections",
            "medium",
            [
                "native calendar/todo entity methods",
                "date/time serialization",
                "stable item IDs",
            ],
        ),
        (
            ("camera",),
            "optional-images",
            "high",
            ["binary image serialization", "entity access", "provider availability"],
        ),
        (
            ("tools_system", "tools_updates", "tools_radio", "tools_themes"),
            "system-and-platform",
            "high",
            [
                "Core vs Supervisor availability",
                "restart/disconnection outcome",
                "native response contracts",
            ],
        ),
        (
            ("dev.py",),
            "developer-controls",
            "high",
            [
                "entry lifecycle ownership",
                "package override scope",
                "no self-deadlock on reload",
            ],
        ),
        (
            ("security_policy",),
            "policy",
            "high",
            ["shared policy engine", "dynamic tool metadata", "policy persistence"],
        ),
        (
            ("bug_report",),
            "issue-reporting",
            "medium",
            ["redaction and diagnostic aggregation", "external publication gate"],
        ),
        (
            ("automation", "script", "scene", "blueprint", "traces"),
            "configuration",
            "high",
            [
                "native validators",
                "YAML and storage ownership",
                "backup/hash/duplicate guards",
                "reload and completion",
            ],
        ),
        (
            ("helper", "integration", "config_flow", "groups"),
            "helpers-and-flows",
            "high",
            [
                "live collection or flow owner",
                "multi-step flow lifecycle",
                "created entity readback",
            ],
        ),
        (
            ("histor", "statistic", "energy", "log"),
            "recorder-and-diagnostics",
            "high",
            [
                "recorder executor ownership",
                "query bounds",
                "native result serialization",
            ],
        ),
        (
            ("dashboard", "resource", "lovelace"),
            "dashboards",
            "high",
            [
                "storage vs YAML mode",
                "optimistic conflict checks",
                "frontend/custom-card discovery",
            ],
        ),
        (
            ("filesystem", "yaml"),
            "privileged-files",
            "high",
            [
                "tools-entry gate",
                "path jail and symlinks",
                "executor I/O",
                "reload validation",
            ],
        ),
        (
            ("code",),
            "custom-code",
            "exception",
            [
                "arbitrary REST/WebSocket ABI",
                "saved custom tools",
                "no retry after mutation",
            ],
        ),
        (
            ("addons", "supervisor", "hacs"),
            "platform-extensions",
            "high",
            [
                "Supervisor availability",
                "optional integration APIs",
                "external-content hints",
            ],
        ),
        (
            (
                "registry",
                "area",
                "device",
                "entit",
                "label",
                "zone",
                "exposure",
                "categor",
            ),
            "registries",
            "medium",
            [
                "Core registry owner",
                "rename cascade",
                "write permissions and attribution",
            ],
        ),
        (
            ("service",),
            "services",
            "high",
            [
                "confirmation listeners before dispatch",
                "partial results",
                "unknown outcome after cancellation",
                "arbitrary ws_command exception",
            ],
        ),
        (
            ("search", "overview", "state"),
            "discovery",
            "medium",
            [
                "aggregate native reads",
                "visibility policy",
                "bounded result serialization",
            ],
        ),
    )
    for fragments, family, risk, reasons in families:
        if any(fragment in path for fragment in fragments):
            return family, risk, reasons
    return (
        "utility-and-cross-cutting",
        "medium",
        ["inspect shared helper dependencies", "preserve result and error contracts"],
    )


def _core_seams() -> list[dict[str, Any]]:
    component = ROOT / "custom_components" / "ha_mcp_tools"
    found = []
    candidates = [
        *sorted((component / "websocket_api").glob("*.py")),
        component / "helper_collections.py",
        component / "core_contract.py",
        component / "dashboard_cards_ws.py",
    ]
    for path in candidates:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [
            {
                "name": node.name,
                "line": node.lineno,
                "async": isinstance(node, ast.AsyncFunctionDef),
            }
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and (
                node.name.startswith("_do_")
                or node.name.endswith("_prep")
                or node.name == "command_specs"
            )
        ]
        if functions:
            found.append(
                {
                    "path": path.relative_to(ROOT).as_posix(),
                    "functions": functions,
                    "wire_literals": _wire_literals(tree),
                }
            )
    return found


def _catalog_call_name(node: ast.AST) -> ast.AST | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "define_tool"
        and len(node.args) >= 3
    ):
        return node.args[2]
    return None


def _literal_batch_names(generator: ast.comprehension | ast.For, name: str) -> set[str]:
    names: set[str] = set()
    if not isinstance(generator.target, (ast.Tuple, ast.List)) or not isinstance(
        generator.iter, (ast.Tuple, ast.List)
    ):
        return names
    for index, target in enumerate(generator.target.elts):
        if not isinstance(target, ast.Name) or target.id != name:
            continue
        for row in generator.iter.elts:
            if not isinstance(row, (ast.Tuple, ast.List)) or len(row.elts) <= index:
                continue
            value = row.elts[index]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                names.add(value.value)
    return names


def _native_declarations(tree: ast.AST) -> set[str]:
    """Read direct names and literal catalog batches without executing the module."""
    declarations = set()
    for node in ast.walk(tree):
        name = _catalog_call_name(node)
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            declarations.add(name.value)
        if isinstance(node, ast.For):
            for child in ast.walk(node):
                name = _catalog_call_name(child)
                if isinstance(name, ast.Name):
                    declarations.update(_literal_batch_names(node, name.id))
        if not isinstance(node, (ast.GeneratorExp, ast.ListComp)):
            continue
        name = _catalog_call_name(node.elt)
        if isinstance(name, ast.Name):
            for generator in node.generators:
                declarations.update(_literal_batch_names(generator, name.id))
    return declarations


def build_inventory() -> dict[str, Any]:
    """Conservative module-level dependency candidates, never proof of porting."""
    extractor = _load_script("extract_tools")
    resolver = _load_script("extract_tools_shared").SharedToolExtractor(
        extractor._import_source,
        extractor._module_scope,
        extractor._extract_tool_from_node,
    )
    catalog = extractor.extract_tools()
    declarations = set().union(
        *(
            _native_declarations(ast.parse(path.read_text(encoding="utf-8")))
            for path in NATIVE_CATALOG_SOURCES
        )
    )
    tools = []
    module_cache: dict[str, dict[str, Any]] = {}
    for tool in sorted(catalog, key=lambda entry: entry["name"]):
        filename = tool["source_file"]
        if filename not in module_cache:
            module_cache[filename] = _module_scan(SOURCE / "tools" / filename)
        method = resolver.resolve_function(SOURCE / "tools" / filename, tool["name"])
        shared_source = getattr(method, "_source_path", SOURCE / "tools" / filename)
        shared_scan = _module_scan(shared_source)
        family, risk, reasons = _family(filename)
        tools.append(
            {
                "name": tool["name"],
                "source": f"src/ha_mcp/tools/{filename}",
                "tags": tool["tags"],
                "beta": "beta" in tool["tags"],
                "family": family,
                "migration_risk": risk,
                "risk_reasons": reasons,
                "native_status": "not_verified",
                "native_catalog_declaration": tool["name"] in declarations,
                "shared_source": shared_source.relative_to(ROOT).as_posix(),
                "semantic_operation_candidates": shared_scan["semantic_operations"],
                "evidence": [],
                "boundary_scope": "containing module; includes sibling tools and excludes unresolved transitive calls",
                "operation_candidates": module_cache[filename]["wire_literals"],
                "client_call_candidates": module_cache[filename]["calls"],
            }
        )
    return {
        "format_version": 1,
        "baseline_revision": BASELINE,
        "source_scope": "current working-tree AST; baseline_revision identifies the immutable preservation reference",
        "method": "AST module-level candidates; neither complete call graph nor effort estimate",
        "status_rule": "not_verified is the default; only matching implementation plus real Core evidence can establish parity",
        "native_catalog_sources": [
            path.relative_to(ROOT).as_posix() for path in NATIVE_CATALOG_SOURCES
        ],
        "counts": {
            "decorated_tools": len(tools),
            "beta_tagged_tools": sum(tool["beta"] for tool in tools),
            "native_catalog_declarations": sum(
                tool["native_catalog_declaration"] for tool in tools
            ),
            "families": dict(sorted(Counter(tool["family"] for tool in tools).items())),
        },
        "tools": tools,
        "existing_core_seams": _core_seams(),
    }


async def _capture_registered_tools() -> list[dict[str, Any]]:
    from ha_mcp import tools as tools_package
    from ha_mcp._vendor.fastmcp import FastMCP
    from ha_mcp.tools import registry

    mcp = FastMCP("native-migration-catalog")
    modules: dict[str, str | None] = {
        item.name: None
        for item in pkgutil.iter_modules(tools_package.__path__)
        if item.name.startswith("tools_")
    }
    modules.update(registry.EXPLICIT_MODULES)
    for name, explicit in sorted(modules.items()):
        module = importlib.import_module(f"ha_mcp.tools.{name}")
        function_name = explicit or next(
            (
                attr
                for attr in dir(module)
                if attr.startswith("register_") and attr.endswith("_tools")
            ),
            None,
        )
        if function_name:
            getattr(module, function_name)(
                mcp,
                MagicMock(),
                smart_tools=MagicMock(),
                device_tools=MagicMock(),
                server=MagicMock(),
            )
    return [
        tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in sorted(await mcp.list_tools(), key=lambda item: item.name)
    ]


async def _capture_server_extras() -> list[dict[str, Any]]:
    from ha_mcp.server import HomeAssistantSmartMCPServer

    server = HomeAssistantSmartMCPServer()
    tool = await server.mcp.get_tool("ha_get_skill_guide")
    if tool is None:
        raise ValueError(
            "The registered server catalogue is missing ha_get_skill_guide"
        )
    contract = tool.to_mcp_tool().model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    # Prose includes bundled frontmatter and a degraded-install notice. A source
    # archive does not contain submodule assets, so it cannot pin that prose.
    contract.pop("description", None)
    return [contract]


def capture_runtime(*, server_extras: bool = False) -> dict[str, Any]:
    """Use actual registration with feature flags, isolated settings and no HA I/O."""
    sys.path.insert(0, str(ROOT / "src"))
    from ha_mcp.config import FEATURE_FLAG_FIELDS, _reset_global_settings
    from ha_mcp.utils.data_paths import get_data_dir

    previous = os.environ.copy()
    try:
        with tempfile.TemporaryDirectory(prefix="ha-mcp-native-catalog-") as temporary:
            os.environ.update(
                {
                    "HOMEASSISTANT_URL": "http://127.0.0.1:9",
                    "HOMEASSISTANT_TOKEN": "unused",
                    "HA_MCP_CONFIG_DIR": temporary,
                    "HA_MCP_DISABLE_SETTINGS_UI": "1",
                }
            )
            for flag in FEATURE_FLAG_FIELDS:
                if flag.ftype is bool and flag.field != "read_only_mode":
                    os.environ[flag.env] = "true"
            os.environ["HAMCP_ENABLE_DEV_MODE"] = "true"
            get_data_dir.cache_clear()
            _reset_global_settings()
            tools = asyncio.run(
                _capture_server_extras()
                if server_extras
                else _capture_registered_tools()
            )
            return {
                "format_version": 1,
                "baseline_revision": BASELINE,
                "scope": "server-generated stable metadata, asset-dependent description excluded"
                if server_extras
                else "raw decorated registry, all boolean feature flags and dev mode; server transforms excluded",
                "tools": tools,
            }
    finally:
        os.environ.clear()
        os.environ.update(previous)
        get_data_dir.cache_clear()
        _reset_global_settings()


def _write_or_check(path: Path, value: dict[str, Any], check: bool) -> bool:
    serialized = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if check:
        matches = path.exists() and path.read_text(encoding="utf-8") == serialized
        if not matches:
            print(f"Inventory drift: {path.relative_to(ROOT)}", file=sys.stderr)
        return matches
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialized, encoding="utf-8", newline="\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--capture-runtime", action="store_true")
    parser.add_argument("--capture-server-extras", action="store_true")
    args = parser.parse_args()
    if args.capture_server_extras:
        result = _write_or_check(
            OUTPUT / "catalog-extras-baseline.json",
            capture_runtime(server_extras=True),
            args.check,
        )
    elif args.capture_runtime:
        result = _write_or_check(
            OUTPUT / "catalog-baseline.json", capture_runtime(), args.check
        )
    else:
        result = _write_or_check(
            OUTPUT / "inventory.json", build_inventory(), args.check
        )
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
