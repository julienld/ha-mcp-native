"""Local fixture-protocol tests. These do NOT qualify Home Assistant or HACS."""

from __future__ import annotations

import importlib.util
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import aiohttp
import pytest

spec = importlib.util.spec_from_file_location(
    "hacs_qualification_content", Path(__file__).parent / "fixture" / "content.py"
)
assert spec and spec.loader
content = importlib.util.module_from_spec(spec)
spec.loader.exec_module(content)


def test_corrupt_archive_writes_one_file_before_failing_crc(tmp_path: Path) -> None:
    with (
        zipfile.ZipFile(io.BytesIO(content.archive("3.0.0", corrupt=True))) as archive,
        pytest.raises(zipfile.BadZipFile, match="CRC"),
    ):
        archive.extractall(tmp_path)
    assert json.loads((tmp_path / "manifest.json").read_text())["version"] == "3.0.0"
    assert (tmp_path / "__init__.py").read_bytes() != content.files("3.0.0")[
        "__init__.py"
    ]


@pytest.mark.asyncio
async def test_offline_routes_feed_real_GitHub_client_models(monkeypatch: Any) -> None:
    # Install this optional setup dependency in an isolated uv environment. A
    # missing client is an error rather than a green skip of the contract probe.
    from aiogithubapi import GitHub, GitHubAPI
    from yarl import URL

    server = content.ContentServer()
    await server.start()
    original = aiohttp.ClientSession._request

    async def routed(session: Any, method: str, str_or_url: Any, **kwargs: Any) -> Any:
        url = URL(str_or_url)
        assert url.host == "api.github.com", url.host
        return await original(
            session, method, server.base + "/" + url.host + url.raw_path_qs, **kwargs
        )

    monkeypatch.setattr(aiohttp.ClientSession, "_request", routed)
    try:
        async with aiohttp.ClientSession(trust_env=False) as session:
            github = GitHub("offline-fixture-not-a-token", session)
            repo = await github.get_repo(content.REPOSITORY)
            assert repo.full_name == content.REPOSITORY
            assert str(repo.attributes["id"]) == content.REPOSITORY_ID
            tree = await repo.get_tree("2.0.0")
            assert any(item.full_path.endswith("manifest.json") for item in tree)
            await repo.set_last_commit()
            assert repo.last_commit
            api = GitHubAPI(token="offline-fixture-not-a-token", session=session)
            releases = await api.repos.releases.list(repository=content.REPOSITORY)
            assert [r.tag_name for r in releases.data] == list(content.VERSIONS)
            manifest = await api.repos.contents.get(
                repository=content.REPOSITORY, path="hacs.json", params={"ref": "2.0.0"}
            )
            assert manifest.data.content
            rate = await api.rate_limit()
            assert rate.data.resources.core.remaining == 5000
        assert not server.unexpected, server.unexpected
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_unknown_network_routes_fail_closed_without_a_proxy() -> None:
    server = content.ContentServer()
    await server.start()
    try:
        async with (
            aiohttp.ClientSession(trust_env=False) as session,
            session.get(
                server.base + "/api.github.com/repos/real/production"
            ) as response,
        ):
            assert response.status == 404
        assert server.unexpected == ["GET api.github.com/repos/real/production"]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_hacs_tag_ref_download_reaches_the_controlled_archive() -> None:
    # HACS 2.0.5 async_install_repository sets ref="tags/<version>" before
    # download_zip_files passes it to this unchanged owner URL builder.
    source = (
        Path(__file__).parents[2]
        / "initial_test_state/custom_components/hacs/utils/url.py"
    )
    spec = importlib.util.spec_from_file_location("hacs_owner_urls", source)
    assert spec and spec.loader
    urls = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(urls)
    url = urls.github_release_asset(
        repository=content.REPOSITORY, version="tags/1.0.0", filename="fixture.zip"
    )
    server = content.ContentServer()
    await server.start()
    try:
        async with (
            aiohttp.ClientSession(trust_env=False) as session,
            session.get(server.base + "/" + url.removeprefix("https://")) as response,
        ):
            assert response.status == 200, await response.text()
            with zipfile.ZipFile(io.BytesIO(await response.read())) as bundle:
                assert json.loads(bundle.read("manifest.json"))["version"] == "1.0.0"
        assert server.archive_entered.is_set()
        assert not server.unexpected, server.unexpected
    finally:
        await server.close()
