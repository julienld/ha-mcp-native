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
                    return bound[1]
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

    def extract(self, node: ast.Assign, path: Path) -> dict | None:
        """Resolve ``method = tool(...)(wrapper(SharedOwner.method))``."""
        bound = self._bound_method(node, path)
        if bound is None:
            return None
        decorator, method, scope = bound
        method = copy.deepcopy(method)
        method.decorator_list = [decorator]
        return self.extract_tool(method, path.name, scope)

    def _bound_method(
        self, node: ast.Assign, path: Path
    ) -> tuple[ast.Call, ast.AsyncFunctionDef, Any] | None:
        value = node.value
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Call):
            return None
        decorator = value.func
        if not isinstance(decorator.func, ast.Name) or decorator.func.id != "tool":
            return None
        for reference in ast.walk(value):
            if not isinstance(reference, ast.Attribute) or not isinstance(
                reference.value, ast.Name
            ):
                continue
            resolved = self._method(path, reference.value.id, reference.attr, set())
            if resolved is None:
                continue
            method, scope = resolved
            return decorator, method, scope
        raise ValueError(
            f"Cannot resolve shared tool registration in {path.name}:{node.lineno}"
        )
