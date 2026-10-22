"""Static caller-parameter lookup for local and shared tool registrations."""

import ast
import runpy
from pathlib import Path


def _is_tool_decorated(node: ast.AST) -> bool:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "tool":
            return True
        if isinstance(target, ast.Name) and target.id == "tool":
            return True
    return False


def decorated_tool_param_names(module_path: Path, tool_name: str) -> set[str]:
    """Return the parameter names of the ``@tool``-decorated function that
    backs ``tool_name`` (both the ``@tool(name=...)`` class-method pattern
    and the ``@mcp.tool(...)`` closure pattern, where the function name IS
    the tool name)."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    scripts = module_path.parents[3] / "scripts"
    extractor = runpy.run_path(str(scripts / "extract_tools.py"))
    shared = runpy.run_path(str(scripts / "extract_tools_shared.py"))
    resolver = shared["SharedToolExtractor"](
        extractor["_import_source"],
        extractor["_module_scope"],
        extractor["_extract_tool_from_node"],
    )
    resolved = resolver.resolve_function(module_path, tool_name)
    if resolved is not None:
        tree.body.append(resolved)

    def _names_match(node: ast.AST) -> bool:
        # @tool(name="ha_...") explicit name kwarg.
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                for kw in dec.keywords:
                    if (
                        kw.arg == "name"
                        and isinstance(kw.value, ast.Constant)
                        and kw.value.value == tool_name
                    ):
                        return True
        # @mcp.tool(...) closure: function name is the tool name.
        return node.name == tool_name and _is_tool_decorated(node)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and _names_match(
            node
        ):
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
            return {a.arg for a in args if a.arg not in ("self", "ctx")}
    raise AssertionError(
        f"could not locate the @tool-decorated function for {tool_name} "
        f"in {module_path.name}"
    )
