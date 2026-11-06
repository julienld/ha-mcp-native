"""UTF-8 config files round-trip even when the process locale is different."""

import io

import pytest

from .test_native_engine_backend import native as native


@pytest.mark.parametrize("direction", ["read", "write"])
def test_file_text_stays_consistent_with_snapshot_bytes_under_ansi_locale(
    native, monkeypatch, tmp_path, direction
):
    from custom_components.ha_mcp_tools import file_io

    target = tmp_path / "automation.yaml"
    text = "alias: Éclairage 漢字 🙂"
    if direction == "read":
        target.write_bytes(text.encode("utf-8"))
    original = io.text_encoding

    def local_encoding(encoding, stacklevel=2):
        return "cp1252" if encoding is None else original(encoding, stacklevel)

    monkeypatch.setattr(io, "text_encoding", local_encoding)
    if direction == "read":
        assert file_io._read_file_sync(target)["content"] == text
    else:
        file_io._write_file_sync(target, text, False, False, tmp_path)
    assert target.read_bytes() == text.encode("utf-8")
    assert file_io.read_file_image(target).text == text
