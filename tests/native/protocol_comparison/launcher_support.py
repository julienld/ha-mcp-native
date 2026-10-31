"""Host-only import seam and bounded public launcher diagnostics."""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from collections import deque
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FRAMES = 12
PHASES = frozenset(
    {
        "worker_start",
        "building_artifacts",
        "verifying_artifacts",
        "creating_network",
        "starting_container",
        "waiting_for_runtime_attestation",
        "ready",
        "stopped",
    }
)


def failure_type(value: Any) -> str:
    return (
        value
        if isinstance(value, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", value)
        else "Exception"
    )


def sanitize_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"phase": "worker_start"}
    phase = value.get("phase")
    result: dict[str, Any] = {
        "phase": phase if isinstance(phase, str) and phase in PHASES else "worker_start"
    }
    if "failure_type" in value:
        result["failure_type"] = failure_type(value["failure_type"])
    if "diagnostic" in value:
        result["diagnostic"] = sanitize_diagnostic(value["diagnostic"])
    return result


def import_worker_helpers(source: Path) -> None:
    """Import real role-local helpers, deferring unrelated SDK assertion exports.

    The broad E2E utilities initializer eagerly re-exports SDK assertions. The
    launcher only uses its stdlib parser/topology submodules. Defer that untouched
    initializer until an export is requested; never stub or install an SDK.
    This process-local seam changes no source file or container artifact.
    """
    source = source.resolve()
    sys.path[:0] = [str(source / "tests/src"), str(source / "tests")]
    package = "e2e.utilities"
    if package not in sys.modules:
        parent = importlib.import_module("e2e")
        initializer = source / "tests/src/e2e/utilities/__init__.py"
        spec = importlib.util.spec_from_file_location(package, initializer)
        if spec is None or spec.loader is None:
            raise ImportError("test_utilities_initializer_required")
        module = importlib.util.module_from_spec(spec)
        loader = spec.loader

        def load_export(name: str) -> Any:
            if name.startswith("__") and name != "__all__":
                raise AttributeError(name)
            del module.__getattr__
            loader.exec_module(module)
            try:
                return vars(module)[name]
            except KeyError:
                raise AttributeError(name) from None

        vars(module)["__getattr__"] = load_export
        sys.modules[package] = module
        vars(parent)["utilities"] = module
    for name in (
        "e2e.utilities",
        "e2e._conftest_embedded",
        "e2e._conftest_native_runtime",
        "e2e._conftest_seed",
        "e2e._conftest_testcontainer",
        "qualification_artifacts",
    ):
        imported = importlib.import_module(name)
        location = imported.__file__
        if location is None or not Path(location).resolve().is_relative_to(
            source / "tests"
        ):
            raise ImportError("worker_helpers_from_another_source")
    if any(
        name in sys.modules for name in ("ha_mcp", "fastmcp", "mcp", "homeassistant")
    ):
        raise ImportError("application_or_sdk_imported_on_host")


def module_name(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 128
        and bool(
            re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*", value)
        )
    )


def public_frame(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    filename, line = value.get("file"), value.get("line")
    if not isinstance(filename, str) or len(filename) > 240:
        return None
    parts = PurePosixPath(filename).parts
    if (
        len(parts) < 2
        or parts[0] not in {"tests", "scripts", "src"}
        or not filename.endswith(".py")
        or any(not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.-]{0,79}", p) for p in parts)
        or type(line) is not int
        or not 0 < line < 1_000_000
    ):
        return None
    return {"file": filename, "line": line}


def sanitize_diagnostic(value: Any) -> dict[str, Any]:
    """Reapply the allowlist when copying private worker state to public JSON."""
    result: dict[str, Any] = {"frames": []}
    if not isinstance(value, dict):
        return result
    if module_name(value.get("missing_module")):
        result["missing_module"] = value["missing_module"]
    frames = value.get("frames")
    if isinstance(frames, list):
        result["frames"] = [
            frame for item in frames[-MAX_FRAMES:] if (frame := public_frame(item))
        ]
    return result


def exception_diagnostic(error: BaseException, *roots: Path) -> dict[str, Any]:
    frames: deque[dict[str, Any]] = deque(maxlen=MAX_FRAMES)
    trace = error.__traceback__
    while trace is not None:
        filename = Path(trace.tb_frame.f_code.co_filename)
        for root in roots:
            if filename.is_absolute() and filename.is_relative_to(root):
                frames.append(
                    {
                        "file": filename.relative_to(root).as_posix(),
                        "line": trace.tb_lineno,
                    }
                )
                break
        trace = trace.tb_next
    result: dict[str, Any] = {"frames": list(frames)}
    if isinstance(error, ModuleNotFoundError):
        result["missing_module"] = error.name
    return sanitize_diagnostic(result)
