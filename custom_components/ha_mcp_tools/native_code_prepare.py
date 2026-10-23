"""Adapt the historical synchronous saved-tool helper to HA's async owner."""

import ast

from ha_mcp.tool_api import ToolFailure


def prepare_native_code(code: str) -> str:
    """Await direct delete_saved_tool calls; reject ambiguous uses before running."""
    tree = ast.parse(code)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    await_calls: set[ast.Call] = set()
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Name)
            or node.id != "delete_saved_tool"
            or not isinstance(node.ctx, ast.Load)
        ):
            continue
        call = parents.get(node)
        if not isinstance(call, ast.Call) or call.func is not node:
            raise _unsupported(
                "Reference delete_saved_tool directly; aliases and callbacks are unavailable in native code mode."
            )
        scope = parents.get(call)
        while scope is not None and not isinstance(
            scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            scope = parents.get(scope)
        if isinstance(scope, (ast.FunctionDef, ast.Lambda)):
            raise _unsupported(
                "Use delete_saved_tool at top level or inside an async function in native code mode."
            )
        if not isinstance(parents.get(call), ast.Await):
            await_calls.add(call)
    if not await_calls:
        return code

    class AwaitDeletes(ast.NodeTransformer):
        def visit_Call(self, node: ast.Call) -> ast.AST:
            self.generic_visit(node)
            return (
                ast.copy_location(ast.Await(value=node), node)
                if node in await_calls
                else node
            )

    return ast.unparse(ast.fix_missing_locations(AwaitDeletes().visit(tree)))


def _unsupported(message: str) -> ToolFailure:
    return ToolFailure("SANDBOX_SYNTAX_UNSUPPORTED", message, status="not_started")
