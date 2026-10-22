"""Public qualification rejects mixed releases before any HA code is started."""

import hashlib
import importlib.util
import io
import json
import stat
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def published_assets():
    spec = importlib.util.spec_from_file_location(
        "published_assets_test", ROOT / "scripts/native_published_assets.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def public_release(tmp_path, published_assets):
    folder = tmp_path / "public"
    folder.mkdir()
    tag = "v9.0.0.dev123"
    version = "9.0.0.dev123"
    wheel = f"ha_mcp-{version}-py3-none-any.whl"
    with zipfile.ZipFile(folder / wheel, "w") as archive:
        archive.writestr(
            f"ha_mcp-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.3\nName: ha-mcp\nVersion: {version}\n",
        )
    wheel_hash = published_assets.digest(folder / wheel)
    dependency = (
        f"ha-mcp @ https://github.com/julienld/ha-mcp-native/releases/download/{tag}/{wheel}"
        f"#sha256={wheel_hash}"
    )
    manifest = json.dumps(
        {
            "domain": "ha_mcp_tools",
            "version": version,
            "requirements": [dependency, "ruamel.yaml>=0.18"],
        }
    ).encode()
    with zipfile.ZipFile(folder / "ha_mcp_tools.zip", "w") as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("__init__.py", "# released source\n")
    proof = {
        "repository": "julienld/ha-mcp-native",
        "revision": "a" * 40,
        "tag": tag,
        "library_version": version,
        "component_version": version,
        "component_protocol_version": "9.0.0",
        "dependency_profile": "native (standalone launchers are optional)",
        "submodules": {"src/vendor": "b" * 40},
        "dependency": dependency,
        "files": {
            wheel: wheel_hash,
            "ha_mcp_tools.zip": published_assets.digest(folder / "ha_mcp_tools.zip"),
        },
    }
    (folder / "native-build.json").write_text(json.dumps(proof))
    return SimpleNamespace(
        folder=folder, proof=proof, tag=tag, manifest=manifest, wheel=wheel
    )


def fixture_download(monkeypatch, published_assets, release):
    calls = []

    def download(tag, filename, destination, maximum):
        assert tag == release.tag
        content = (release.folder / filename).read_bytes()
        assert len(content) <= maximum
        destination.write_bytes(content)
        calls.append(filename)

    monkeypatch.setattr(published_assets, "download", download)
    monkeypatch.setattr(
        published_assets,
        "checkout_provenance",
        lambda _: ("a" * 40, {"src/vendor": "b" * 40}),
    )
    return calls


def test_public_archive_reaches_image_unchanged_without_preinstalled_wheel(
    tmp_path, monkeypatch, published_assets, public_release
):
    calls = fixture_download(monkeypatch, published_assets, public_release)
    output = tmp_path / "downloaded"
    proof = published_assets.fetch(tmp_path, output, public_release.tag)
    assert set(calls) == {"native-build.json", public_release.wheel, "ha_mcp_tools.zip"}
    # The image receives only these assets, never the wheel downloaded by the host.
    (output / public_release.wheel).unlink()
    component = tmp_path / "image" / "custom_components" / "ha_mcp_tools"
    published_assets.prepare_image(output, component, "a" * 40)
    assert (component / "manifest.json").read_bytes() == public_release.manifest
    assert (
        hashlib.sha256(public_release.manifest).hexdigest() == proof["manifest_sha256"]
    )
    assert not list(component.rglob("*.whl"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("revision", "c" * 40),
        ("repository", "someone/else"),
        ("submodules", {"src/vendor": "c" * 40}),
        ("dependency", "ha-mcp==9.0.0.dev123"),
    ],
)
def test_mixed_provenance_is_rejected_before_binary_download(
    tmp_path, monkeypatch, published_assets, public_release, field, value
):
    public_release.proof[field] = value
    (public_release.folder / "native-build.json").write_text(
        json.dumps(public_release.proof)
    )
    calls = fixture_download(monkeypatch, published_assets, public_release)
    with pytest.raises(ValueError):
        published_assets.fetch(tmp_path, tmp_path / "download", public_release.tag)
    assert calls == ["native-build.json"]


@pytest.mark.parametrize("asset", ["wheel", "zip"])
def test_changed_public_bytes_fail_digest_check(
    tmp_path, monkeypatch, published_assets, public_release, asset
):
    name = public_release.wheel if asset == "wheel" else "ha_mcp_tools.zip"
    with (public_release.folder / name).open("ab") as stream:
        stream.write(b"changed")
    fixture_download(monkeypatch, published_assets, public_release)
    with pytest.raises(ValueError, match="SHA-256"):
        published_assets.fetch(tmp_path, tmp_path / "download", public_release.tag)


def test_valid_archive_digest_does_not_authorize_a_rewritten_manifest(
    published_assets, public_release
):
    path = public_release.folder / "ha_mcp_tools.zip"
    manifest = json.loads(public_release.manifest)
    manifest["requirements"][0] = "ha-mcp==9.0.0.dev123"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    public_release.proof["files"][path.name] = published_assets.digest(path)
    contract = published_assets.release_contract(
        public_release.proof, public_release.tag, "a" * 40
    )
    with pytest.raises(ValueError, match="manifest"):
        published_assets.verify_component(path, public_release.proof, contract)


def test_image_rechecks_host_receipt_before_extracting_code(
    tmp_path, monkeypatch, published_assets, public_release
):
    fixture_download(monkeypatch, published_assets, public_release)
    output = tmp_path / "downloaded"
    receipt = published_assets.fetch(tmp_path, output, public_release.tag)
    receipt["source_revision"] = "c" * 40
    (output / "published-verification.json").write_text(json.dumps(receipt))
    destination = tmp_path / "component"
    with pytest.raises(ValueError, match="host receipt"):
        published_assets.prepare_image(output, destination, "a" * 40)
    assert not destination.exists()


@pytest.mark.parametrize(
    "name,mode",
    [
        ("../outside", stat.S_IFREG),
        ("/outside", stat.S_IFREG),
        ("a\\outside", stat.S_IFREG),
        ("link", stat.S_IFLNK),
    ],
)
def test_verified_archive_still_cannot_escape_component(
    tmp_path, published_assets, public_release, name, mode
):
    path = public_release.folder / "ha_mcp_tools.zip"
    with zipfile.ZipFile(path, "a") as archive:
        entry = zipfile.ZipInfo(name)
        # Windows ZipInfo normalizes separators on construction; retain malicious
        # archive bytes explicitly so this test also exercises Linux extraction.
        entry.filename = name
        entry.external_attr = (mode | 0o644) << 16
        archive.writestr(entry, "outside")
    public_release.proof["files"][path.name] = published_assets.digest(path)
    contract = published_assets.release_contract(
        public_release.proof, public_release.tag, "a" * 40
    )
    with pytest.raises(ValueError, match="Unsafe"):
        published_assets.verify_component(path, public_release.proof, contract)
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize(
    "tag",
    ["../other", "release/next", "tag?query", "$(echo unsafe)", "https://example.test"],
)
def test_tag_cannot_turn_fixed_public_download_into_another_url(published_assets, tag):
    with pytest.raises(ValueError):
        published_assets.validate_tag(tag)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/file",
        "https://example.test/file",
        "https://github.com.evil.test/file",
        "https://token@github.com/file",
        "https://release-assets.githubusercontent.com:444/file",
    ],
)
def test_redirect_rejected_before_opening_an_untrusted_destination(
    published_assets, url
):
    handler = published_assets.AssetRedirect()
    request = urllib.request.Request(
        "https://github.com/julienld/ha-mcp-native/releases/download/v1/file"
    )
    with pytest.raises(ValueError, match="outside GitHub"):
        handler.redirect_request(request, None, 302, "Found", {}, url)


@pytest.mark.parametrize("declared", [True, False])
def test_download_size_limit_applies_even_when_server_omits_length(
    tmp_path, monkeypatch, published_assets, declared
):
    response = io.BytesIO(b"too much")
    response.url = "https://release-assets.githubusercontent.com/asset"
    response.headers = {"Content-Length": "8"} if declared else {}
    monkeypatch.setattr(
        published_assets.urllib.request,
        "build_opener",
        lambda _: SimpleNamespace(open=lambda *args, **kwargs: response),
    )
    with pytest.raises(ValueError, match="limit"):
        published_assets.download("v1", "asset", tmp_path / "asset", 4)


def test_missing_asset_failure_does_not_expose_signed_redirect_query(
    tmp_path, monkeypatch, published_assets
):
    def fail(*args, **kwargs):
        raise published_assets.urllib.error.URLError(
            "https://release-assets.githubusercontent.com/a?sig=private-value"
        )

    monkeypatch.setattr(
        published_assets.urllib.request,
        "build_opener",
        lambda _: SimpleNamespace(open=fail),
    )
    with pytest.raises(RuntimeError) as caught:
        published_assets.download("v1", "asset", tmp_path / "asset", 4)
    assert "private-value" not in str(caught.value)
    assert "unavailable" in str(caught.value)


def test_download_uses_only_public_request_and_preserves_bytes(
    tmp_path, monkeypatch, published_assets
):
    response = io.BytesIO(b"published bytes")
    response.url = "https://release-assets.githubusercontent.com/asset?sig=ephemeral"
    response.headers = {}
    requests = []

    def open_asset(request, *, timeout):
        requests.append(request)
        assert timeout > 0
        return response

    monkeypatch.setattr(
        published_assets.urllib.request,
        "build_opener",
        lambda _: SimpleNamespace(open=open_asset),
    )
    destination = tmp_path / "asset"
    published_assets.download("v1", "native-build.json", destination, 1024)
    assert destination.read_bytes() == b"published bytes"
    assert (
        requests[0].full_url
        == "https://github.com/julienld/ha-mcp-native/releases/download/v1/native-build.json"
    )
    assert not requests[0].has_header("Authorization")


def test_download_cumulative_budget_rejects_trickling_bytes(
    tmp_path, monkeypatch, published_assets
):
    response = io.BytesIO(b"late")
    response.url = "https://release-assets.githubusercontent.com/asset"
    response.headers = {}
    clock = iter((0, 121))
    monkeypatch.setattr(published_assets.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(
        published_assets.urllib.request,
        "build_opener",
        lambda _: SimpleNamespace(open=lambda *a, **k: response),
    )
    with pytest.raises(ValueError, match="time limit"):
        published_assets.download("v1", "asset", tmp_path / "asset", 1024)
    assert (tmp_path / "asset").read_bytes() == b""
