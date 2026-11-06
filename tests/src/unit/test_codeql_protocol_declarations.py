"""Quality exceptions recognize declarations without hiding executable code."""

import json

import pytest

from .test_codeql_quality_gate import _result, _write_sarif, gate


@pytest.mark.parametrize(
    ("source", "line"),
    [
        (
            "from typing import Protocol\nclass Port(Protocol):\n    def read(self): ...\n",
            3,
        ),
        (
            "import typing as t\nclass Port(t.Protocol):\n    async def read(self):\n        'Contract documentation'\n        ...\n",
            5,
        ),
        (
            "from typing_extensions import Protocol as P\nfrom typing import TypeVar\nT = TypeVar('T')\nclass Port(P[T]):\n    def read(self): ...\n",
            5,
        ),
    ],
)
def test_protocol_declaration_is_reported_with_an_explicit_reason(
    tmp_path, capsys, source, line
):
    path = tmp_path / "contract.py"
    path.write_text(source)
    sarif = _write_sarif(
        tmp_path,
        [
            _result(
                "py/ineffectual-statement",
                str(path),
                line,
                "This statement has no effect",
            )
        ],
    )
    assert gate.main(["gate", str(sarif)]) == 0
    output = capsys.readouterr().out
    assert "Suppressed" in output and "Protocol" in output and "Ellipsis" in output


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ("...\n", 1),
        ("class Runtime:\n    def read(self): ...\n", 2),
        ("from typing import Protocol\nclass Port(Protocol):\n    ...\n", 3),
        (
            "from typing import Protocol\nclass Port(Protocol):\n    def read(self):\n        42\n        ...\n",
            5,
        ),
        (
            "from typing import Protocol\nclass Port(Protocol):\n    def read(self):\n        return ...\n",
            4,
        ),
        ("class Protocol: pass\nclass Port(Protocol):\n    def read(self): ...\n", 3),
        (
            "from typing import Protocol\nProtocol = object\nclass Port(Protocol):\n    def read(self): ...\n",
            4,
        ),
        (
            "import typing as t\nt.Protocol = object\nclass Port(t.Protocol):\n    def read(self): ...\n",
            4,
        ),
        (
            "from typing import Protocol\ndel Protocol\nclass Port(Protocol):\n    def read(self): ...\n",
            4,
        ),
        (
            "from typing import Protocol\nif enabled:\n    from somewhere import Protocol\nclass Port(Protocol):\n    def read(self): ...\n",
            5,
        ),
        (
            "from typing import Protocol\nif enabled:\n    class Protocol: pass\nclass Port(Protocol):\n    def read(self): ...\n",
            5,
        ),
        (
            "from somewhere import Protocol\nclass Port(Protocol):\n    def read(self): ...\n",
            3,
        ),
        (
            "from typing import Protocol\nclass Port(Protocol):\n    def read(self): ...\n    broken syntax\n",
            3,
        ),
    ],
)
def test_executable_or_unverified_ellipsis_still_blocks(tmp_path, source, line):
    path = tmp_path / "runtime.py"
    path.write_text(source)
    sarif = _write_sarif(
        tmp_path,
        [
            _result(
                "py/ineffectual-statement",
                str(path),
                line,
                "This statement has no effect",
            )
        ],
    )
    findings, suppressed = gate.classify(sarif)
    assert len(findings) == 1 and not suppressed


def test_protocol_exception_never_suppresses_another_rule(tmp_path):
    path = tmp_path / "contract.py"
    path.write_text(
        "from typing import Protocol\nclass Port(Protocol):\n    def read(self): ...\n"
    )
    sarif = _write_sarif(
        tmp_path,
        [
            _result(
                "py/clear-text-logging-sensitive-data",
                str(path),
                3,
                "This statement has no effect",
            )
        ],
    )
    findings, suppressed = gate.classify(sarif)
    assert len(findings) == 1 and not suppressed


def test_rich_quality_exclusion_keeps_security_unknown_rules_and_owned_siblings(
    tmp_path,
):
    quality = "py/unused-import"
    security = "py/clear-text-logging-sensitive-data"
    results = [
        _result(quality, "src/ha_mcp/_vendor/rich/table.py", 1, "third-party quality"),
        _result(security, "src/ha_mcp/_vendor/rich/table.py", 2, "security"),
        _result(
            "py/unknown", "src/ha_mcp/_vendor/rich/table.py", 3, "no rule metadata"
        ),
        _result(quality, "src/ha_mcp/_vendor/rich_helpers.py", 1, "owned sibling"),
        _result(quality, "src/ha_mcp/_vendor/__init__.py", 1, "owned bootstrap"),
    ]
    sarif = _write_sarif(tmp_path, results)
    content = json.loads(sarif.read_text())
    content["runs"][0]["tool"]["driver"]["rules"] = [
        {"id": quality, "properties": {"tags": ["quality"]}},
        {
            "id": security,
            "properties": {"tags": ["quality", "security"], "security-severity": "7.5"},
        },
    ]
    sarif.write_text(json.dumps(content))
    findings, suppressed = gate.classify(sarif)
    assert {finding[3] for finding in findings} == {
        "security",
        "no rule metadata",
        "owned sibling",
        "owned bootstrap",
    }
    assert not suppressed
