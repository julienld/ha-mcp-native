"""Canonical HACS delivery preserves the paired bytes and immutable identity."""

import json
import shutil
import tomllib
import zipfile
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from packaging.requirements import Requirement

from .test_native_release import release_builder as release_builder
from .test_native_release import source as source


@pytest.fixture
def delivery():
    from scripts import native_release_delivery

    return native_release_delivery


@pytest.fixture(params=["9.0.0", "9.0.0.dev12"])
def bundle(request, source, tmp_path, release_builder):
    version = request.param
    output = tmp_path / "native"
    proof = release_builder.build(
        source, output, version, f"v{version}", "homeassistant-ai/ha-mcp"
    )
    return output, proof


def test_canonical_delivery_checks_the_produced_component_and_wheel(delivery, bundle):
    output, proof = bundle
    verified = delivery.verify_bundle(
        output, proof["repository"], proof["tag"], "a" * 40
    )
    assert verified == proof


def test_delivery_refuses_another_source_before_publication(delivery, bundle):
    output, proof = bundle
    with pytest.raises(ValueError, match="source checkout"):
        delivery.verify_bundle(output, proof["repository"], proof["tag"], "c" * 40)


def test_delivery_rejects_non_object_provenance(delivery, bundle):
    output, proof = bundle
    (output / "native-build.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        delivery.verify_bundle(output, proof["repository"], proof["tag"], "a" * 40)


@pytest.mark.parametrize("revision", [None, 123, [], "not-a-commit"])
def test_delivery_rejects_an_invalid_release_revision(delivery, revision):
    with pytest.raises(ValueError, match="stamp revision"):
        delivery.release_revision({"revision": "a" * 40, "release_revision": revision})


def test_mirror_extracts_exact_published_bytes_without_restamping(
    delivery, bundle, tmp_path
):
    output, proof = bundle
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    delivery.stage_mirror(output, mirror, proof["repository"], proof["tag"], "a" * 40)
    with zipfile.ZipFile(output / "ha_mcp_tools.zip") as archive:
        for name in archive.namelist():
            assert (
                mirror / "custom_components/ha_mcp_tools" / name
            ).read_bytes() == archive.read(name)
    assert (mirror / "native-build.json").read_bytes() == (
        output / "native-build.json"
    ).read_bytes()


class Github:
    def __init__(self, bundle):
        self.output, self.proof = bundle
        self.release = None
        self.payloads = {}
        self.writes = []
        self.listings = None
        self.after_create = lambda: None
        self.after_upload = lambda: None

    def api(self, endpoint, *, missing=False):
        if "/commits/" in endpoint:
            return {"sha": "a" * 40}
        raise AssertionError(f"Unexpected API read: {endpoint}")

    def command(self, *args):
        if args[:2] == ("api", "--paginate"):
            pages = (
                self.listings()
                if self.listings
                else [[self.release] if self.release else []]
            )
            return json.dumps(pages)
        assert args[0] == "api"
        method = args[args.index("--method") + 1]
        self.writes.append(args)
        fields = dict(
            args[index + 1].split("=", 1)
            for index, arg in enumerate(args[:-1])
            if arg in ("-f", "-F")
        )
        if method == "POST" and args[1].endswith("/releases"):
            repository = args[1].removeprefix("repos/").removesuffix("/releases")
            assert fields["draft"] == "true"
            assert (
                fields["prerelease"]
                == str(".dev" in self.proof["library_version"]).lower()
            )
            assert fields["target_commitish"] in ("a" * 40, "d" * 40)
            assert fields["name"] == fields["tag_name"]
            assert Path(fields["body"].removeprefix("@")).is_file()
            self.release = {
                "id": 1,
                "tag_name": fields["tag_name"],
                "upload_url": f"https://uploads.github.com/repos/{repository}/releases/1/assets{{?name,label}}",
                "draft": True,
                "prerelease": fields["prerelease"] == "true",
                "assets": [],
            }
            self.after_create()
        elif method == "POST" and args[1].startswith("https://uploads.github.com/"):
            path = Path(args[args.index("--input") + 1])
            url = urlsplit(args[1])
            assert url.path.endswith(f"/releases/{self.release['id']}/assets")
            assert parse_qs(url.query) == {"name": [path.name]}
            assert "Content-Type: application/octet-stream" in args
            self.payloads[path.name] = path.read_bytes()
            self.release["assets"].append({"id": len(self.payloads), "name": path.name})
            self.after_upload()
        elif method == "PATCH":
            assert args[1].endswith(f"/releases/{self.release['id']}")
            assert fields == {"draft": "false"}
            self.release["draft"] = False
        else:
            pytest.fail(f"Unexpected API write: {args}")
        return json.dumps(self.release)

    def download(self, repository, asset, destination):
        destination.write_bytes(self.payloads[asset["name"]])


@pytest.fixture
def github(monkeypatch, delivery, bundle):
    remote = Github(bundle)
    monkeypatch.setattr(delivery, "api", remote.api)
    monkeypatch.setattr(delivery, "gh", remote.command)
    monkeypatch.setattr(delivery, "download_asset", remote.download)
    return remote


def publish(delivery, bundle, tmp_path, *, draft_only=False):
    output, proof = bundle
    notes = tmp_path / "notes.md"
    notes.write_text("Release notes")
    delivery.publish(
        output,
        proof["repository"],
        proof["tag"],
        "a" * 40,
        notes,
        draft_only=draft_only,
    )


def test_release_lookup_searches_all_pages_including_pending_drafts(
    delivery, monkeypatch
):
    selected = {
        "id": 7,
        "tag_name": "v9.0.0",
        "draft": True,
        "prerelease": False,
        "assets": [],
    }

    def command(*args):
        assert args[0] == "api" and "--paginate" in args and "--slurp" in args
        assert args[-1].partition("?")[0] == "repos/owner/fork/releases"
        return json.dumps([[dict(selected, id=6, tag_name="v8.0.0")], [selected]])

    monkeypatch.setattr(delivery, "gh", command)
    assert delivery.release_by_tag("owner/fork", "v9.0.0") == selected


@pytest.mark.parametrize(
    "pages", [None, [None], [[None]], [[{}]], [[{"tag_name": "v9.0.0", "id": True}]]]
)
def test_malformed_release_listing_is_not_absence(delivery, monkeypatch, pages):
    monkeypatch.setattr(delivery, "gh", lambda *args: json.dumps(pages))
    with pytest.raises(ValueError, match="release"):
        delivery.release_by_tag("owner/fork", "v9.0.0", missing=True)


def test_release_lookup_distinguishes_absence_from_failed_reads(delivery, monkeypatch):
    monkeypatch.setattr(delivery, "gh", lambda *args: "[[]]")
    assert delivery.release_by_tag("owner/fork", "v9.0.0", missing=True) is None
    with pytest.raises(RuntimeError, match="not found"):
        delivery.release_by_tag("owner/fork", "v9.0.0")

    def failed(*args):
        raise RuntimeError("release listing unavailable")

    monkeypatch.setattr(delivery, "gh", failed)
    with pytest.raises(RuntimeError, match="unavailable"):
        delivery.release_by_tag("owner/fork", "v9.0.0", missing=True)


def test_duplicate_pending_drafts_block_all_release_writes(
    delivery, bundle, github, tmp_path
):
    publish(delivery, bundle, tmp_path, draft_only=True)
    github.listings = lambda: [[deepcopy(github.release)], [dict(github.release, id=2)]]
    github.writes.clear()
    with pytest.raises(ValueError, match="Multiple releases"):
        publish(delivery, bundle, tmp_path)
    assert github.writes == []


@pytest.mark.parametrize(
    "defect", ["absent", "error", "duplicate", "replacement", "published", "channel"]
)
def test_unverified_creation_never_uploads_or_publishes(
    delivery, bundle, github, tmp_path, defect
):
    def after_create():
        if defect == "error":

            def failed():
                raise RuntimeError("release listing unavailable")

            github.listings = failed
        elif defect == "absent":
            github.listings = lambda: [[]]
        elif defect == "duplicate":
            github.listings = lambda: [[github.release, dict(github.release, id=2)]]
        else:
            replacement = deepcopy(github.release)
            if defect == "replacement":
                replacement["id"] = 2
            elif defect == "published":
                replacement["draft"] = False
            else:
                replacement["prerelease"] = not replacement["prerelease"]
            github.listings = lambda: [[replacement]]

    github.after_create = after_create
    with pytest.raises((RuntimeError, ValueError), match=r"release|channel"):
        publish(delivery, bundle, tmp_path)
    assert len(github.writes) == 1
    assert github.payloads == {}


@pytest.mark.parametrize("defect", ["replacement", "channel", "bytes"])
def test_upload_readback_must_keep_the_selected_identity_channel_and_bytes(
    delivery, bundle, github, tmp_path, defect
):
    def after_upload():
        if len(github.payloads) != len(bundle[1]["files"]) + 1:
            return
        selected = deepcopy(github.release)
        if defect == "replacement":
            selected["id"] = 2
        elif defect == "channel":
            selected["prerelease"] = not selected["prerelease"]
        else:
            github.payloads["ha_mcp_tools.zip"] = b"damaged upload"
        github.listings = lambda: [[selected]]

    github.after_upload = after_upload
    with pytest.raises(ValueError, match=r"release|differs"):
        publish(delivery, bundle, tmp_path)
    assert github.release["draft"]
    assert all("PATCH" not in call for call in github.writes)


@pytest.mark.parametrize(
    "upload_url",
    [
        None,
        "https://untrusted.test/upload",
        "https://uploads.github.com/repos/homeassistant-ai/ha-mcp/releases/2/assets{?name,label}",
    ],
)
def test_upload_url_must_identify_the_selected_release(
    delivery, bundle, github, tmp_path, upload_url
):
    github.after_create = lambda: github.release.update(upload_url=upload_url)
    with pytest.raises(ValueError, match="upload URL"):
        publish(delivery, bundle, tmp_path)
    assert len(github.writes) == 1
    assert github.payloads == {}


def test_empty_existing_draft_requires_original_build_artifact(
    delivery, bundle, github, tmp_path, monkeypatch
):
    publish(delivery, bundle, tmp_path, draft_only=True)
    github.release["assets"].clear()
    monkeypatch.setattr(
        delivery,
        "build_bundle",
        lambda *a: pytest.fail("must not rebuild an existing identity"),
    )
    proof = bundle[1]
    with pytest.raises(ValueError, match="Incomplete existing release"):
        delivery.prepare(
            Path.cwd(),
            tmp_path / "recovered",
            proof["library_version"],
            proof["tag"],
            proof["repository"],
            proof["revision"],
        )


def test_release_becomes_visible_only_after_all_three_assets_verify(
    delivery, bundle, github, tmp_path
):
    publish(delivery, bundle, tmp_path)
    assert not github.release["draft"]
    assert set(github.payloads) == {*bundle[1]["files"], "native-build.json"}
    assert github.writes[-1][:2] == (
        "api",
        f"repos/{bundle[1]['repository']}/releases/1",
    )
    assert all(call[0] == "api" for call in github.writes)
    github.writes.clear()
    publish(delivery, bundle, tmp_path)
    assert github.writes == []


def test_complete_pending_draft_is_reused_without_a_second_creation(
    delivery, bundle, github, tmp_path
):
    publish(delivery, bundle, tmp_path, draft_only=True)
    publish(delivery, bundle, tmp_path)
    assert len([call for call in github.writes if call[1].endswith("/releases")]) == 1
    assert github.release["draft"] is False
    assert set(github.payloads) == {*bundle[1]["files"], "native-build.json"}


def test_changed_asset_is_never_clobbered_even_in_a_draft(
    delivery, bundle, github, tmp_path
):
    publish(delivery, bundle, tmp_path, draft_only=True)
    github.payloads["ha_mcp_tools.zip"] = b"different release"
    github.writes.clear()
    with pytest.raises(ValueError, match="differs"):
        publish(delivery, bundle, tmp_path)
    assert github.writes == []
    assert github.release["draft"]


def test_incomplete_public_release_is_not_silently_repaired(
    delivery, bundle, github, tmp_path
):
    publish(delivery, bundle, tmp_path)
    github.release["assets"].pop()
    github.writes.clear()
    with pytest.raises(ValueError, match=r"Published release.*missing"):
        publish(delivery, bundle, tmp_path)
    assert github.writes == []


def test_rerun_reuses_published_bytes_without_rebuilding(
    delivery, bundle, github, tmp_path, monkeypatch
):
    publish(delivery, bundle, tmp_path)
    output, proof = bundle
    monkeypatch.setattr(
        delivery,
        "build_bundle",
        lambda *a, **k: pytest.fail("must reuse immutable assets"),
    )
    recovered = tmp_path / "recovered"
    delivery.prepare(
        Path.cwd(),
        recovered,
        proof["library_version"],
        proof["tag"],
        proof["repository"],
        "a" * 40,
    )
    for name in github.payloads:
        assert (recovered / name).read_bytes() == (output / name).read_bytes()


def test_mirror_rejects_changed_upstream_provenance(delivery, bundle, tmp_path):
    output, proof = bundle
    recorded = tmp_path / "recorded.json"
    shutil.copyfile(output / "native-build.json", recorded)
    changed = dict(proof, builder_sha256="c" * 64)
    (output / "native-build.json").write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="recorded provenance"):
        delivery.verify_recorded(output, recorded)


def test_partial_draft_resumes_only_with_the_original_bytes(
    delivery, bundle, github, tmp_path
):
    publish(delivery, bundle, tmp_path, draft_only=True)
    lost = github.release["assets"].pop()["name"]
    github.writes.clear()
    publish(delivery, bundle, tmp_path)
    uploads = [
        args
        for args in github.writes
        if args[1].startswith("https://uploads.github.com/")
    ]
    assert len(uploads) == 1 and parse_qs(urlsplit(uploads[0][1]).query) == {
        "name": [lost]
    }
    assert not github.release["draft"]


def test_mirror_fetch_binds_downloaded_assets_to_the_git_record(
    delivery, bundle, github, tmp_path
):
    publish(delivery, bundle, tmp_path)
    output, _ = bundle
    fetched = tmp_path / "mirror-assets"
    delivery.fetch_recorded(output / "native-build.json", fetched)
    assert {path.name for path in fetched.iterdir()} == set(github.payloads)
    for name, payload in github.payloads.items():
        assert (fetched / name).read_bytes() == payload


def test_mirror_never_exposes_an_upstream_draft(delivery, bundle, github, tmp_path):
    publish(delivery, bundle, tmp_path, draft_only=True)
    with pytest.raises(ValueError, match="public release"):
        delivery.fetch_recorded(bundle[0] / "native-build.json", tmp_path / "blocked")
    assert not (tmp_path / "blocked").exists()


def test_mirror_release_keeps_its_tag_convention_and_original_wheel_url(
    delivery, bundle, github, tmp_path
):
    directory, proof = bundle
    tag = "v" + proof["library_version"].replace(".dev", "-dev.")
    notes = tmp_path / "notes.md"
    notes.write_text("Mirror release")
    delivery.publish(
        directory,
        "homeassistant-ai/ha-mcp-integration",
        tag,
        "a" * 40,
        notes,
        recorded=directory / "native-build.json",
    )
    assert not github.release["draft"]
    assert f"tag_name={tag}" in github.writes[0]
    for name, payload in github.payloads.items():
        assert payload == (directory / name).read_bytes()


@pytest.mark.parametrize("bundle", ["9.0.0.dev12"], indirect=True)
def test_publication_and_mirror_follow_the_attested_tag_but_keep_the_original_code_sha(
    delivery, bundle, github, tmp_path, monkeypatch
):
    directory, proof = bundle
    proof["release_revision"] = "d" * 40
    (directory / "native-build.json").write_text(json.dumps(proof))
    original_api = github.api

    def api(endpoint, **kwargs):
        if "/commits/" in endpoint:
            return {"sha": "d" * 40}
        return original_api(endpoint, **kwargs)

    monkeypatch.setattr(delivery, "api", api)
    notes = tmp_path / "notes.md"
    notes.write_text("Dev release")
    delivery.publish(directory, proof["repository"], proof["tag"], "d" * 40, notes)
    fetched = tmp_path / "mirror-fetch"
    delivery.fetch_recorded(directory / "native-build.json", fetched)
    recovered = json.loads((fetched / "native-build.json").read_bytes())
    assert recovered["revision"] == "a" * 40
    assert recovered["release_revision"] == "d" * 40


@pytest.mark.parametrize("field", ["manifest", "protocol", "index"])
def test_delivery_refuses_internally_mixed_archives_even_with_valid_zip_hash(
    delivery, bundle, field
):
    directory, proof = bundle
    path = directory / "ha_mcp_tools.zip"
    with zipfile.ZipFile(path) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    if field == "manifest":
        manifest = json.loads(files["manifest.json"])
        manifest["version"] = "99.0.0"
        files["manifest.json"] = json.dumps(manifest).encode()
    elif field == "protocol":
        files["const.py"] = b'COMPONENT_VERSION = "99.0.0"\n'
    else:
        index = json.loads(files["native-wheel.json"])
        index["files"]["ha_mcp/__init__.py"]["sha256"] = "0" * 64
        files["native-wheel.json"] = json.dumps(index).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    proof["files"][path.name] = delivery.assets.digest(path)
    (directory / "native-build.json").write_text(json.dumps(proof))
    with pytest.raises(ValueError, match=r"manifest|protocol|payload bytes"):
        delivery.verify_bundle(
            directory, proof["repository"], proof["tag"], proof["revision"]
        )


def test_active_wheel_dependency_edges_block_canonical_delivery(
    delivery, source, release_builder, tmp_path, monkeypatch
):
    select = release_builder.select_native_dependencies

    def broken_backend(project):
        requirements = select(project)
        project.write_text(
            project.read_text().replace(
                "dependencies = []", 'dependencies = ["httpx>=0.27"]'
            )
        )
        return requirements

    monkeypatch.setattr(release_builder, "select_native_dependencies", broken_backend)
    output = tmp_path / "bad-backend"
    proof = release_builder.build(
        source, output, "9.0.0", "v9.0.0", "homeassistant-ai/ha-mcp"
    )
    with pytest.raises(ValueError, match="active dependency edges"):
        delivery.verify_bundle(
            output, proof["repository"], proof["tag"], proof["revision"]
        )


def test_security_certifi_requirement_reaches_the_native_manifest_unchanged(
    source, release_builder, tmp_path
):
    project = Path(__file__).resolve().parents[3] / "pyproject.toml"
    shutil.copyfile(project, source / "pyproject.toml")
    requirements = tomllib.loads(project.read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    expected = next(
        value for value in requirements if Requirement(value).name == "certifi"
    )
    output = tmp_path / "certifi-native"
    release_builder.build(source, output, "9.0.0", "v9.0.0", "homeassistant-ai/ha-mcp")
    with zipfile.ZipFile(output / "ha_mcp_tools.zip") as archive:
        published = json.loads(archive.read("manifest.json"))["requirements"]
    assert [value for value in published if Requirement(value).name == "certifi"] == [
        expected
    ]


def test_changed_remote_tag_blocks_publication_before_any_release_write(
    delivery, bundle, github, tmp_path, monkeypatch
):
    monkeypatch.setattr(delivery, "api", lambda *args, **kwargs: {"sha": "c" * 40})
    with pytest.raises(ValueError, match="another source revision"):
        publish(delivery, bundle, tmp_path)
    assert github.writes == []


@pytest.mark.parametrize("bundle", ["9.0.0.dev12"], indirect=True)
def test_hacs_source_discovery_and_installed_zip_report_the_same_dev_build(
    delivery, bundle, source
):
    before = {
        path.relative_to(source).as_posix(): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    directory, proof = bundle
    delivery.stage_dev_source(source, directory)
    with zipfile.ZipFile(directory / "ha_mcp_tools.zip") as archive:
        for name in ("manifest.json", "const.py"):
            assert (
                source / "custom_components/ha_mcp_tools" / name
            ).read_bytes() == archive.read(name)
    manifest = json.loads(
        (source / "custom_components/ha_mcp_tools/manifest.json").read_bytes()
    )
    assert manifest["version"] == proof["library_version"]
    assert proof["dependency"] in manifest["requirements"]
    assert (
        f'COMPONENT_VERSION = "{proof["component_protocol_version"]}"'
        in (source / "custom_components/ha_mcp_tools/const.py").read_text()
    )
    changed = {
        name for name, data in before.items() if (source / name).read_bytes() != data
    }
    assert changed == {
        "custom_components/ha_mcp_tools/manifest.json",
        "custom_components/ha_mcp_tools/const.py",
    }


@pytest.mark.parametrize("bundle", ["9.0.0.dev12"], indirect=True)
@pytest.mark.parametrize(
    "defect", [None, "code", "parent", "metadata", "replaced-stamp"]
)
def test_dev_tag_attestation_binds_only_the_metadata_stamp_to_the_built_source(
    delivery, bundle, source, monkeypatch, defect
):
    directory, proof = bundle
    delivery.stage_dev_source(source, directory)
    stamped = "d" * 40
    if defect == "replaced-stamp":
        proof["release_revision"] = "e" * 40
        (directory / "native-build.json").write_text(json.dumps(proof))

    def git_bytes(checkout, *args):
        assert checkout == source
        if args[:3] == ("show", "-s", "--format=%P"):
            return (
                ("b" * 40 if defect == "parent" else proof["revision"]) + "\n"
            ).encode()
        if args[:2] == ("diff", "--name-only"):
            names = "custom_components/ha_mcp_tools/manifest.json\ncustom_components/ha_mcp_tools/const.py\n"
            return (
                names + ("src/ha_mcp/server.py\n" if defect == "code" else "")
            ).encode()
        payload = (source / args[1].split(":", 1)[1]).read_bytes()
        return payload + b"\n# wrong stamp" if defect == "metadata" else payload

    monkeypatch.setattr(delivery, "git_bytes", git_bytes)
    if defect:
        with pytest.raises(ValueError, match=r"stamp|metadata"):
            delivery.attest_dev_source(source, directory, stamped)
    else:
        delivery.attest_dev_source(source, directory, stamped)
        attested = json.loads((directory / "native-build.json").read_bytes())
        assert attested["revision"] == proof["revision"]
        assert delivery.release_revision(attested) == stamped
        # A second attestation cannot change even the provenance JSON bytes.
        first = (directory / "native-build.json").read_bytes()
        delivery.attest_dev_source(source, directory, stamped)
        assert (directory / "native-build.json").read_bytes() == first
