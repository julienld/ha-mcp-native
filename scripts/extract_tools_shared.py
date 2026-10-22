"""Static resolution of shared tool methods for the documentation extractor."""

from __future__ import annotations

import ast
import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any


class SharedToolExtractor:
    """Read only project-owned AST using the main extractor's confined imports."""

    def __init__(
        self,
        import_source: Callable[[Path, ast.ImportFrom], Path | None],
        module_scope: Callable[[Path], Any],
        extract_tool: Callable[[ast.AsyncFunctionDef, str, Any], dict | None],
    ) -> None:
        self.import_source = import_source
        self.module_scope = module_scope
        self.extract_tool = extract_tool

    def _method(
        self,
        path: Path,
        class_name: str,
        method_name: str,
        seen: set[tuple[Path, str, str]],
    ) -> tuple[ast.AsyncFunctionDef, Any] | None:
        identity = (path, class_name, method_name)
        if identity in seen:
            return None
        seen.add(identity)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return self._class_method(path, node, method_name, seen)
        return self._imported_method(path, tree, class_name, method_name, seen)

    def _class_method(
        self,
        path: Path,
        owner: ast.ClassDef,
        method_name: str,
        seen: set[tuple[Path, str, str]],
    ) -> tuple[ast.AsyncFunctionDef, Any] | None:
        for member in owner.body:
            if isinstance(member, ast.AsyncFunctionDef) and member.name == method_name:
                member._source_path = path
                return member, self.module_scope(path)
        for base in owner.bases:
            if isinstance(base, ast.Name):
                resolved = self._method(path, base.id, method_name, seen)
                if resolved is not None:
                    return resolved
        return None

    def _imported_method(
        self,
        path: Path,
        tree: ast.Module,
        class_name: str,
        method_name: str,
        seen: set[tuple[Path, str, str]],
    ) -> tuple[ast.AsyncFunctionDef, Any] | None:
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            source = self.import_source(path, node)
            if source is None:
                continue
            for alias in node.names:
                if (alias.asname or alias.name) == class_name:
                    return self._method(source, alias.name, method_name, seen)
        return None

    def resolve_function(
        self, path: Path, name: str, seen: set[tuple[Path, str]] | None = None
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        """Return an imported function or decorated method's actual source AST."""
        visited = set() if seen is None else seen
        if (path, name) in visited:
            return None
        visited.add((path, name))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
            ):
                node._source_path = path
                return node
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name
                for target in node.targets
            ):
                bound = self._bound_method(node, path)
                if bound is not None:
                    method = copy.deepcopy(bound[1])
                    method.decorator_list = [bound[0]]
                    return method
            if isinstance(node, ast.Expr):
                bound = self._bound_method(node, path)
                if bound is not None and bound[1].name == name:
                    method = copy.deepcopy(bound[1])
                    method.decorator_list = [bound[0]]
                    return method
        return self._imported_function(path, tree, name, visited)

    def _imported_function(
        self, path: Path, tree: ast.Module, name: str, seen: set[tuple[Path, str]]
    ) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue
            source = self.import_source(path, node)
            if source is None:
                continue
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return self.resolve_function(source, alias.name, seen)
        return None

    def extract(self, node: ast.Assign | ast.Expr, path: Path) -> dict | None:
        """Resolve decorator application to a shared class or instance method."""
        bound = self._bound_method(node, path)
        if bound is None:
            return None
        decorator, method, scope = bound
        method = copy.deepcopy(method)
        method.decorator_list = [decorator]
        return self.extract_tool(method, path.name, scope)

    def _bound_method(
        self, node: ast.Assign | ast.Expr, path: Path
    ) -> tuple[ast.Call, ast.AsyncFunctionDef, Any] | None:
        value = node.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Call):
            return None
        decorator = value.func
        function = decorator.func
        if not (
            (isinstance(function, ast.Name) and function.id == "tool")
            or (isinstance(function, ast.Attribute) and function.attr == "tool")
        ):
            return None
        for reference in ast.walk(value):
            if not isinstance(reference, ast.Attribute) or not isinstance(
                reference.value, ast.Name
            ):
                continue
            resolved = self._method(path, reference.value.id, reference.attr, set())
            if resolved is None:
                owner = self._instance_owner(path, node, reference.value.id)
                if owner is not None:
                    resolved = self._method(path, owner, reference.attr, set())
            if resolved is None:
                continue
            method, scope = resolved
            return decorator, method, scope
        raise ValueError(
            f"Cannot resolve shared tool registration in {path.name}:{node.lineno}"
        )

    @staticmethod
    def _instance_owner(path: Path, registration: ast.AST, name: str) -> str | None:
        """Resolve an unambiguous ``instance = SharedClass(...)`` in its scope."""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scopes = [
            item
            for item in ast.walk(tree)
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and item.lineno <= registration.lineno <= (item.end_lineno or item.lineno)
        ]
        scope = (
            min(scopes, key=lambda item: (item.end_lineno or item.lineno) - item.lineno)
            if scopes
            else tree
        )
        constructors = {
            item.value.func.id
            for item in ast.walk(scope)
            if isinstance(item, ast.Assign)
            and item.lineno < registration.lineno
            and any(
                isinstance(target, ast.Name) and target.id == name
                for target in item.targets
            )
            and isinstance(item.value, ast.Call)
            and isinstance(item.value.func, ast.Name)
        }
        return next(iter(constructors)) if len(constructors) == 1 else None
