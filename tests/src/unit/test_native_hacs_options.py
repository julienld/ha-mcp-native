"""Optional native store reads and explicit Marketplace replacement consent."""

import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .test_native_engine_backend import native as native
from .test_native_hacs import call
from .test_native_hacs import hacs as hacs
from .test_native_marketplace import marketplace as marketplace
from .test_native_marketplace import update_entity as update_entity


@pytest.fixture
def release_reads(hacs, monkeypatch):
    release = SimpleNamespace(
        name="Version two",
        tag_name="2",
        published_at="2026-10-31",
        prerelease=False,
        body="Native release notes",
    )
    hacs.repo.async_get_releases = AsyncMock(return_value=[release])
    hacs.repo.releases = SimpleNamespace(objects=[release])
    for suffix in ("releases", "release_notes"):
        hacs.hass.data["websocket_api"][f"hacs/repository/{suffix}"] = (
            object(),
            lambda value: value,
        )
    monkeypatch.setitem(
        sys.modules,
        "custom_components.hacs.utils.version",
        SimpleNamespace(
            version_left_higher_then_right=lambda left, right: left != right
        ),
    )
    return hacs


async def test_hacs_optional_releases_use_owner_after_normal_info_refresh(
    release_reads,
):
    result = await call(release_reads, "info", repository_id="1", include_releases=True)
    assert not result.is_error, result.data
    data = result.data["data"]
    assert data["github_releases"][0]["tag"] == "2"
    assert data["release_notes"] == [
        {"name": "Version two", "tag": "2", "body": "Native release notes"}
    ]
    release_reads.repo.async_get_releases.assert_awaited_once_with()
    release_reads.repo.update_repository.assert_awaited_once_with(
        ignore_issues=True, force=True
    )
    release_reads.owner.data.async_write.assert_awaited_once()
    assert not release_reads.repo.data.new


async def test_release_failure_is_warning_beside_cached_repository(release_reads):
    release_reads.repo.async_get_releases.side_effect = RuntimeError(
        "GitHub unavailable"
    )
    result = await call(release_reads, "info", repository_id="1", include_releases=True)
    assert not result.is_error, result.data
    assert result.data["data"]["data"]["id"] == "1"
    assert "GitHub unavailable" in " ".join(result.data["warnings"])
    assert "warnings" not in result.data["data"]


async def test_releases_recheck_actor_before_returning_data(release_reads):
    async def fetch():
        release_reads.state.active = False
        return []

    release_reads.repo.async_get_releases.side_effect = fetch
    result = await call(release_reads, "info", repository_id="1", include_releases=True)
    assert result.is_error and result.status == "not_started", result.data
    release_reads.repo.async_get_releases.assert_awaited_once()
    assert result.data["error"]["code"] == "AUTH_FORBIDDEN"
    assert "github_releases" not in str(result.data)


async def test_marketplace_replacement_confirmation_reaches_core(marketplace):
    async def install(*, ref=None, confirm_replace_built_in=False):
        assert confirm_replace_built_in is True
        await marketplace.repo._download(ref=ref)

    marketplace.repo.data.installed = False
    marketplace.repo.async_install_repository.side_effect = install
    result = await call(
        marketplace, "download", repository_id="1", confirm_replace_built_in=True
    )
    assert not result.is_error, result.data
    marketplace.repo.async_install_repository.assert_awaited_once_with(
        ref=None, confirm_replace_built_in=True
    )


async def test_hacs_does_not_receive_marketplace_only_confirmation(hacs):
    result = await call(
        hacs, "download", repository_id="1", confirm_replace_built_in=True
    )
    assert not result.is_error, result.data
    hacs.repo.async_download_repository.assert_awaited_once_with(ref=None)


async def test_confirmation_still_rechecks_consent_at_dispatch(marketplace):
    async def revoke(*args):
        marketplace.owner.warning_accepted.return_value = False

    marketplace.manager.visibility.before.side_effect = revoke
    result = await call(
        marketplace, "download", repository_id="1", confirm_replace_built_in=True
    )
    assert result.is_error and result.status == "not_started", result.data
    assert result.data["error"]["code"] == "AUTH_FORBIDDEN"
    marketplace.repo.async_install_repository.assert_not_awaited()


async def test_confirmed_install_failure_is_unknown_and_never_replayed(marketplace):
    marketplace.repo.async_install_repository.side_effect = RuntimeError(
        "metadata changed, reply lost"
    )
    result = await call(
        marketplace, "download", repository_id="1", confirm_replace_built_in=True
    )
    assert result.is_error and result.status == "outcome_unknown", result.data
    marketplace.repo.async_install_repository.assert_awaited_once()


async def test_marketplace_release_notes_use_the_pinned_update_owner(
    marketplace, update_entity
):
    marketplace.repo.async_get_releases = AsyncMock(return_value=[])
    marketplace.hass.data["websocket_api"]["marketplace/repository/releases"] = (
        object(),
        lambda value: value,
    )
    update_entity.entity.async_release_notes = AsyncMock(
        return_value="Core formatted release notes"
    )
    result = await call(marketplace, "info", repository_id="1", include_releases=True)
    assert not result.is_error, result.data
    assert result.data["data"]["release_notes"] == "Core formatted release notes"
    update_entity.entity.async_release_notes.assert_awaited_once_with()


async def test_reinstallation_with_update_entity_does_not_drop_confirmation(
    marketplace, update_entity
):
    async def install(*, ref=None, confirm_replace_built_in=False):
        assert confirm_replace_built_in
        await marketplace.repo._download(ref=ref)

    marketplace.repo.data.installed = False
    marketplace.repo.async_install_repository.side_effect = install
    result = await call(
        marketplace, "download", repository_id="1", confirm_replace_built_in=True
    )
    assert not result.is_error, result.data
    marketplace.repo.async_install_repository.assert_awaited_once_with(
        ref=None, confirm_replace_built_in=True
    )
    marketplace.hass.services.async_call.assert_not_awaited()


async def test_release_notes_recheck_repository_link_after_admission(
    marketplace, update_entity, monkeypatch
):
    from custom_components.ha_mcp_tools.native_operations import hacs_updates

    marketplace.repo.async_get_releases = AsyncMock(return_value=[])
    marketplace.hass.data["websocket_api"]["marketplace/repository/releases"] = (
        object(),
        lambda value: value,
    )
    update_entity.entity.async_release_notes = AsyncMock(
        return_value="Wrong repository notes"
    )
    original = hacs_updates.repository_update_target
    found = False

    def select(access, repository):
        nonlocal found
        result = original(access, repository)
        found = True
        return result

    async def credential():
        if found:
            update_entity.entity.repository = object()

    monkeypatch.setattr(hacs_updates, "repository_update_target", select)
    marketplace.actor = replace(marketplace.actor, credential=credential)
    result = await call(marketplace, "info", repository_id="1", include_releases=True)
    assert result.is_error and result.data["error"]["code"] == "CONFIG_CONFLICT", (
        result.data
    )
    update_entity.entity.async_release_notes.assert_not_awaited()
