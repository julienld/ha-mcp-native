"""Failed atomic saves preserve the prior file and remove partial temporary files."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from ha_mcp.runtime import code_persistence


@pytest.mark.parametrize("failure", ["dump_io", "dump_type", "replace"])
def test_failed_save_removes_temporary_file_and_preserves_prior_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path = tmp_path / "saved.json"
    path.write_text("previous content", encoding="utf-8")
    if failure == "replace":
        monkeypatch.setattr(Path, "replace", Mock(side_effect=OSError("disk full")))
    else:
        monkeypatch.setattr(
            code_persistence.json,
            "dump",
            Mock(
                side_effect=(
                    TypeError("not serializable")
                    if failure == "dump_type"
                    else OSError("disk full")
                )
            ),
        )
    assert code_persistence.SavedToolsPersistence().save(str(path), {}) is False
    assert path.read_text(encoding="utf-8") == "previous content"
    assert list(tmp_path.iterdir()) == [path]
