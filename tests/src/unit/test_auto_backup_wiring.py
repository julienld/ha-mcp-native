"""Write tools retain their actual capture wrapper after façade extraction."""


def test_destructive_tools_carry_auto_backup_decorator() -> None:
    """Source guard: the three destructive tools this PR wired must keep
    their @with_auto_backup decorator — a deleted line would silently stop
    capturing without failing any behavioral test."""
    import ast
    from pathlib import Path

    tools_dir = Path(__file__).resolve().parents[3] / "src" / "ha_mcp" / "tools"
    # (fname, func, domain, id_param-or-None, mandatory). The file/YAML writes
    # (#1579) are mandatory; ha_config_set_yaml computes its key via id_fn so it
    # has no id_param to assert.
    checks = [
        (
            "tools_entities.py",
            "ha_remove_entity",
            "entity_registry",
            "entity_id",
            False,
        ),
        ("tools_registry.py", "ha_set_device", "device", "device_id", False),
        ("tools_registry.py", "ha_remove_device", "device", "device_id", False),
        ("tools_filesystem.py", "ha_write_file", "file", "path", True),
        ("tools_filesystem.py", "ha_delete_file", "file", "path", True),
        ("tools_yaml_config.py", "ha_config_set_yaml", "yaml", None, True),
    ]
    for fname, func, domain, id_param, mandatory in checks:
        tree = ast.parse((tools_dir / fname).read_text(encoding="utf-8"))
        declaration = next(
            node
            for node in ast.walk(tree)
            if (isinstance(node, ast.AsyncFunctionDef) and node.name == func)
            or (
                isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == func
                    for target in node.targets
                )
            )
        )
        wrappers = (
            declaration.decorator_list
            if isinstance(declaration, ast.AsyncFunctionDef)
            else [declaration.value]
        )
        capture = next(
            (
                node
                for wrapper in wrappers
                for node in ast.walk(wrapper)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "with_auto_backup"
            ),
            None,
        )
        assert capture is not None, f"{func} lost @with_auto_backup"
        keywords = {item.arg: item.value for item in capture.keywords}
        assert ast.literal_eval(keywords["domain"]) == domain, (
            f"{func} wrong backup domain"
        )
        if id_param is not None:
            assert ast.literal_eval(keywords["id_param"]) == id_param, (
                f"{func} wrong backup identity"
            )
        if mandatory:
            assert ast.literal_eval(keywords["mandatory"]) is True, (
                f"{func} lost mandatory=True"
            )
