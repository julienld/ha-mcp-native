"""Server-entry setup and the options added for the HACS review (#2427).

Split from test_config_flow.py, whose flow fakes and Home Assistant stubs it
reuses.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .test_config_flow import _make_flow, _make_options_flow, cf, const


class TestServerBranch:
    """New native entries bind a chosen HA owner without issuing credentials."""

    OWNER = "selected-owner"

    @pytest.fixture(autouse=True)
    def qualified_native_core(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cf, "HA_VERSION", "2026.10.2")

    def _flow(self):
        flow = _make_flow()
        flow.hass.auth.async_get_users = AsyncMock(
            return_value=[
                SimpleNamespace(
                    id=self.OWNER,
                    name="Selected owner",
                    is_active=True,
                    is_admin=True,
                    system_generated=False,
                )
            ]
        )
        return flow

    def _defaults(self, form) -> dict:
        return {
            cf.SETUP_REMOTE_ACCESS: cf.REMOTE_ACCESS_DISABLED,
            const.DATA_NATIVE_OWNER_ID: self.OWNER,
        }

    def test_server_step_shows_owner_and_remote_access(self):
        flow = self._flow()
        form = asyncio.run(flow.async_step_server(None))
        assert form["type"] == "form"
        fields = {marker.schema for marker in form["data_schema"].schema}
        assert const.DATA_NATIVE_OWNER_ID in fields
        assert cf.SETUP_ADMIN_TOKEN not in fields
        assert const.OPT_BIND_HOST not in fields

    def test_new_install_defaults_to_assist_only_native_runtime(self):
        flow = self._flow()
        form = asyncio.run(flow.async_step_server(None))
        entry = asyncio.run(flow.async_step_server(self._defaults(form)))
        assert entry["type"] == "entry"
        assert entry["data"][const.CONF_ENTRY_TYPE] == const.ENTRY_TYPE_SERVER
        assert entry["options"][const.OPT_ENABLE_WEBHOOK] is False
        assert entry["options"][const.OPT_RUNTIME_MODE] == const.RUNTIME_NATIVE
        assert entry["data"][const.DATA_NATIVE_OWNER_ID] == self.OWNER
        assert const.DATA_ADMIN_TOKEN not in entry["data"]
        flow.hass.auth.async_create_access_token.assert_not_called()

    @pytest.mark.parametrize(
        "mode",
        [const.WEBHOOK_AUTH_NONE, const.WEBHOOK_AUTH_HA, const.WEBHOOK_AUTH_LEGACY],
    )
    def test_choosing_remote_access_preserves_auth_modes(self, mode):
        flow = self._flow()
        entry = asyncio.run(
            flow.async_step_server(
                {
                    cf.SETUP_REMOTE_ACCESS: mode,
                    const.DATA_NATIVE_OWNER_ID: self.OWNER,
                }
            )
        )
        assert entry["options"][const.OPT_ENABLE_WEBHOOK] is True
        assert entry["options"][const.OPT_WEBHOOK_AUTH] == mode

    def test_disabled_remote_access_keeps_ha_sign_in_for_later_enable(self):
        flow = self._flow()
        entry = asyncio.run(flow.async_step_server(self._defaults(None)))
        assert entry["options"][const.OPT_WEBHOOK_AUTH] == const.WEBHOOK_AUTH_HA

    def test_previous_token_setup_payload_resolves_owner_without_retaining_token(self):
        flow = self._flow()
        flow.hass.auth.async_validate_access_token.return_value = SimpleNamespace(
            user=SimpleNamespace(id=self.OWNER)
        )
        entry = asyncio.run(
            flow.async_step_server({cf.SETUP_ADMIN_TOKEN: "old-payload-token"})
        )
        assert entry["data"][const.DATA_NATIVE_OWNER_ID] == self.OWNER
        assert const.DATA_ADMIN_TOKEN not in entry["data"]
        flow.hass.auth.async_create_access_token.assert_not_called()

    def test_expired_token_setup_payload_does_not_select_an_owner(self):
        flow = self._flow()
        flow.hass.auth.async_validate_access_token.return_value = None
        result = asyncio.run(flow.async_step_server({cf.SETUP_ADMIN_TOKEN: "expired"}))
        assert result["type"] == "form"

    @pytest.mark.parametrize("owner", ["", "missing"])
    def test_unselected_owner_keeps_form_open(self, owner):
        flow = self._flow()
        result = asyncio.run(
            flow.async_step_server({const.DATA_NATIVE_OWNER_ID: owner})
        )
        assert result["type"] == "form"
        assert result["errors"] == {const.DATA_NATIVE_OWNER_ID: "token_not_admin"}

    @pytest.mark.parametrize(
        "restriction", ["is_admin", "is_active", "system_generated"]
    )
    def test_owner_must_be_a_live_human_administrator(self, restriction):
        flow = self._flow()
        user = flow.hass.auth.async_get_users.return_value[0]
        setattr(user, restriction, restriction == "system_generated")
        result = asyncio.run(flow.async_step_server(self._defaults(None)))
        assert result["type"] == "form"

    def test_an_abandoned_setup_does_not_block_a_new_one(self):
        flow = self._flow()

        async def claim(unique_id, *, raise_on_progress=True):
            if raise_on_progress:
                raise RuntimeError("already_in_progress")

        flow.async_set_unique_id = claim
        assert asyncio.run(flow.async_step_server(None))["type"] == "form"

    def test_server_refuses_a_second_entry_on_every_submit(self):
        class AlreadyConfigured(Exception):
            pass

        flow = self._flow()
        flow._abort_if_unique_id_configured.side_effect = AlreadyConfigured
        with pytest.raises(AlreadyConfigured):
            asyncio.run(flow.async_step_server(self._defaults(None)))
        flow.async_create_entry.assert_not_called()

    def test_server_preserves_the_existing_unique_id(self):
        flow = self._flow()
        asyncio.run(flow.async_step_server(None))
        assert flow.async_set_unique_id.await_args.args == (cf._SERVER_UNIQUE_ID,)
        assert cf._SERVER_UNIQUE_ID != const.DOMAIN

    def test_a_setup_that_lost_the_owner_lookup_race_aborts_without_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class AlreadyConfigured(Exception):
            pass

        flow = self._flow()
        flow._abort_if_unique_id_configured.side_effect = [None, AlreadyConfigured()]
        issue = AsyncMock()
        release = AsyncMock()
        monkeypatch.setattr(cf.server_credentials, "async_credential_from_input", issue)
        monkeypatch.setattr(cf.server_credentials, "async_release_credentials", release)

        with pytest.raises(AlreadyConfigured):
            asyncio.run(flow.async_step_server(self._defaults(None)))

        flow.async_create_entry.assert_not_called()
        issue.assert_not_awaited()
        release.assert_not_awaited()

    def test_server_aborts_on_unsupported_home_assistant(self, monkeypatch):
        monkeypatch.setattr(cf, "HA_VERSION", "2025.9.4")
        flow = self._flow()
        result = asyncio.run(flow.async_step_server(None))
        assert result["reason"] == "unsupported_home_assistant"
        flow.async_set_unique_id.assert_not_awaited()


class TestOAuthCallbackAllowlistOption:
    """The Configure screen edits the none-mode callback allowlist (#2427)."""

    CALLBACK = "https://chatgpt.example/cb"

    def _suggested(self, form):
        marker = next(
            m
            for m in form["data_schema"].schema
            if m.schema == const.OPT_OAUTH_REDIRECT_ALLOWLIST
        )
        return marker.description["suggested_value"]

    def test_an_unedited_entry_shows_the_default_list(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        assert self._suggested(form) == list(const.DEFAULT_OAUTH_REDIRECT_ALLOWLIST)

    def test_saves_the_entered_callbacks(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        result = asyncio.run(
            flow.async_step_init(
                {const.OPT_OAUTH_REDIRECT_ALLOWLIST: [f" {self.CALLBACK} "]}
            )
        )
        assert result["data"][const.OPT_OAUTH_REDIRECT_ALLOWLIST] == [self.CALLBACK]

    def test_an_unusable_callback_blocks_the_save(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        result = asyncio.run(
            flow.async_step_init(
                {const.OPT_OAUTH_REDIRECT_ALLOWLIST: ["http://not-loopback/cb"]}
            )
        )
        assert result["errors"] == {
            const.OPT_OAUTH_REDIRECT_ALLOWLIST: "invalid_oauth_callback"
        }
        # With several callbacks listed, the error names the one to fix.
        assert result["description_placeholders"]["invalid_callbacks"] == (
            "http://not-loopback/cb"
        )

    def test_more_callbacks_than_the_cap_block_the_save(self):
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        many = [f"{self.CALLBACK}{n}" for n in range(const.MAX_OAUTH_CALLBACKS + 1)]
        result = asyncio.run(
            flow.async_step_init({const.OPT_OAUTH_REDIRECT_ALLOWLIST: many})
        )
        assert result["errors"] == {
            const.OPT_OAUTH_REDIRECT_ALLOWLIST: "too_many_oauth_callbacks"
        }

    def test_saving_the_untouched_default_keeps_following_it(self):
        # Pinning the default would hide callbacks a later release adds to it.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        result = asyncio.run(
            flow.async_step_init(
                {
                    const.OPT_OAUTH_REDIRECT_ALLOWLIST: list(
                        const.DEFAULT_OAUTH_REDIRECT_ALLOWLIST
                    )
                }
            )
        )
        assert const.OPT_OAUTH_REDIRECT_ALLOWLIST not in result["data"]

    def test_a_configure_save_that_changes_only_the_list_needs_no_reload(self):
        # The frontend resubmits every field; with only the list changed, the
        # update listener must see no reload-relevant difference (#2427).
        import custom_components.ha_mcp_tools.embedded_entry as entry_mod

        first = asyncio.run(
            _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"}).async_step_init(
                {const.OPT_OAUTH_REDIRECT_ALLOWLIST: [self.CALLBACK]}
            )
        )["data"]
        second = asyncio.run(
            _make_options_flow(
                options=first, data={const.DATA_WEBHOOK_ID: "mcp_abc"}
            ).async_step_init(
                {
                    **first,
                    const.OPT_OAUTH_REDIRECT_ALLOWLIST: ["https://other.example/cb"],
                }
            )
        )["data"]
        assert second != first
        assert entry_mod._reload_relevant(second) == entry_mod._reload_relevant(first)

    def test_the_allow_any_switch_saves_without_a_reload(self) -> None:
        # Read per request like the list, so turning it on needs no reload.
        import custom_components.ha_mcp_tools.embedded_entry as entry_mod

        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        first = asyncio.run(flow.async_step_init({}))["data"]
        flow = _make_options_flow(
            options=first, data={const.DATA_WEBHOOK_ID: "mcp_abc"}
        )
        second = asyncio.run(
            flow.async_step_init({**first, const.OPT_OAUTH_ALLOW_ANY_CALLBACK: True})
        )["data"]
        assert second[const.OPT_OAUTH_ALLOW_ANY_CALLBACK] is True
        assert entry_mod._reload_relevant(second) == entry_mod._reload_relevant(first)

    def test_removing_every_callback_sticks(self):
        # Home Assistant's frontend omits an emptied optional field.
        flow = _make_options_flow(
            options={const.OPT_OAUTH_REDIRECT_ALLOWLIST: [self.CALLBACK]},
            data={const.DATA_WEBHOOK_ID: "mcp_abc"},
        )
        result = asyncio.run(flow.async_step_init({}))
        assert result["data"][const.OPT_OAUTH_REDIRECT_ALLOWLIST] == []


class TestAdminTokenReplacement:
    """Configure replaces the server's administrator token (#2427)."""

    @pytest.fixture(autouse=True)
    def _token_check(self, monkeypatch):
        monkeypatch.setattr(
            cf.server_credentials,
            "token_problem",
            lambda _hass, token: None if token == "good" else "token_not_admin",
        )

    def _marker(self, form):
        return next(
            m
            for m in form["data_schema"].schema
            if m.schema == const.OPT_ADMIN_TOKEN_REPLACEMENT
        )

    def test_the_login_fields_never_offer_a_saved_login(self) -> None:
        # A browser would autofill the administrator's own saved login, which
        # must never go there; a filled login also switches the server's account.
        flow = _make_options_flow(data={const.DATA_WEBHOOK_ID: "mcp_abc"})
        form = asyncio.run(flow.async_step_init(None))
        fields = {m.schema: v for m, v in form["data_schema"].schema.items()}
        assert fields[cf.server_credentials.ADMIN_USERNAME].config.autocomplete == "off"
        assert fields[cf.server_credentials.ADMIN_PASSWORD].config.autocomplete == (
            "new-password"
        )

    def test_the_stored_token_is_never_shown(self):
        flow = _make_options_flow(
            options={const.OPT_ADMIN_TOKEN_REPLACEMENT: "good"},
            data={const.DATA_ADMIN_TOKEN: "secret"},
        )
        form = asyncio.run(flow.async_step_init(None))
        assert not (self._marker(form).description or {}).get("suggested_value")

    def _token_flow(self, options=None):
        flow = _make_options_flow(options=options, data={"webhook_id": "w"})
        flow.config_entry.entry_id = "srv1"
        return flow

    def test_a_working_token_goes_into_the_entry_data_not_the_options(self):
        # config_entries/get returns the options, so the token must never
        # pass through them.
        flow = self._token_flow()
        result = asyncio.run(
            flow.async_step_init({const.OPT_ADMIN_TOKEN_REPLACEMENT: " good "})
        )
        assert const.OPT_ADMIN_TOKEN_REPLACEMENT not in result["data"]
        update = flow.hass.config_entries.async_update_entry.call_args
        assert update.kwargs["data"] == {"webhook_id": "w", "admin_token": "good"}

    def test_a_token_only_save_restarts_the_server_with_it(self):
        # Unchanged options do not reload the entry by themselves.
        saved = asyncio.run(self._token_flow().async_step_init({}))["data"]
        flow = self._token_flow(options=saved)
        asyncio.run(flow.async_step_init({const.OPT_ADMIN_TOKEN_REPLACEMENT: "good"}))
        flow.hass.config_entries.async_schedule_reload.assert_called_once_with("srv1")

    def test_a_save_that_changes_options_leaves_the_reload_to_the_listener(self):
        flow = self._token_flow()
        asyncio.run(flow.async_step_init({const.OPT_ADMIN_TOKEN_REPLACEMENT: "good"}))
        flow.hass.config_entries.async_schedule_reload.assert_not_called()

    def test_an_unusable_token_blocks_the_save(self):
        flow = _make_options_flow()
        result = asyncio.run(
            flow.async_step_init({const.OPT_ADMIN_TOKEN_REPLACEMENT: "bad"})
        )
        assert result["errors"] == {
            const.OPT_ADMIN_TOKEN_REPLACEMENT: "token_not_admin"
        }

    def test_an_accounts_login_switches_the_server_and_is_never_stored(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            cf.server_credentials,
            "async_credential_from_input",
            AsyncMock(return_value=("issued-tok", "i1")),
        )
        flow = self._token_flow()
        result = asyncio.run(
            flow.async_step_init(
                {
                    cf.server_credentials.ADMIN_USERNAME: "ha-mcp",
                    cf.server_credentials.ADMIN_PASSWORD: "pw",
                }
            )
        )
        assert cf.server_credentials.ADMIN_USERNAME not in result["data"]
        assert cf.server_credentials.ADMIN_PASSWORD not in result["data"]
        update = flow.hass.config_entries.async_update_entry.call_args
        assert update.kwargs["data"] == {
            "webhook_id": "w",
            "admin_token": "issued-tok",
            "issued_token_id": "i1",
        }

    def test_leaving_it_empty_keeps_the_current_token(self):
        flow = _make_options_flow()
        result = asyncio.run(flow.async_step_init({}))
        assert const.OPT_ADMIN_TOKEN_REPLACEMENT not in result["data"]


class TestOptionsFlowDispatch:
    def test_server_entry_gets_server_options_flow(self):
        entry = SimpleNamespace(data={const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_SERVER})
        result = cf.HaMcpToolsConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, cf.HaMcpServerOptionsFlow)

    def test_tools_entry_gets_info_options_flow(self):
        entry = SimpleNamespace(data={const.CONF_ENTRY_TYPE: const.ENTRY_TYPE_TOOLS})
        result = cf.HaMcpToolsConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, cf.HaMcpToolsInfoOptionsFlow)

    def test_missing_entry_type_defaults_to_info_options(self):
        # A pre-existing (pre-fold) tools entry carries no entry_type key; it must
        # be treated as "tools", so it gets the informational tools flow, never
        # the server options flow.
        entry = SimpleNamespace(data={})
        result = cf.HaMcpToolsConfigFlow.async_get_options_flow(entry)
        assert isinstance(result, cf.HaMcpToolsInfoOptionsFlow)
        assert not isinstance(result, cf.HaMcpServerOptionsFlow)


class TestOAuthCredsHint:
    """All three branches of the admin-only credentials hint on the options
    form (review gap: zero coverage on any of them)."""

    def test_non_legacy_mode_points_at_the_mode_selector(self) -> None:
        flow = _make_options_flow(
            options={const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_NONE}
        )
        hint = flow._oauth_creds_hint(cf._COMMON_FALLBACKS)
        assert "Set Authentication mode" in hint
        assert "Client ID" in hint

    def test_legacy_mode_before_minting_says_appear_after_start(self) -> None:
        flow = _make_options_flow(
            options={const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY}
        )
        hint = flow._oauth_creds_hint(cf._COMMON_FALLBACKS)
        assert "once the server" in hint

    def test_legacy_mode_live_creds_shows_values_without_caveat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bound AND live (post-restart steady state): no restart caveat.
        monkeypatch.setattr(cf, "_legacy_credentials_active", lambda *a: True)
        monkeypatch.setattr(cf, "_legacy_restart_pending", lambda *a: False)
        flow = _make_options_flow(
            options={const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY},
            data={
                const.DATA_OAUTH_CLIENT_ID: "hamcp-abc",
                const.DATA_OAUTH_CLIENT_SECRET: "s3cr3t",
            },
        )
        hint = flow._oauth_creds_hint(cf._COMMON_FALLBACKS)
        assert "Client ID: hamcp-abc" in hint
        assert "Client Secret: s3cr3t" in hint
        assert "not serving these yet" not in hint

    def test_legacy_mode_first_enable_carries_not_live_caveat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Review finding: mid-session first enable binds the current identity
        # (active True) but /authorize is not live until the restart -- the
        # hint must caveat, matching the startup log's first-enable branch.
        monkeypatch.setattr(cf, "_legacy_credentials_active", lambda *a: True)
        monkeypatch.setattr(cf, "_legacy_restart_pending", lambda *a: True)
        flow = _make_options_flow(
            options={const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY},
            data={
                const.DATA_OAUTH_CLIENT_ID: "hamcp-abc",
                const.DATA_OAUTH_CLIENT_SECRET: "s3cr3t",
            },
        )
        hint = flow._oauth_creds_hint(cf._COMMON_FALLBACKS)
        assert "Client ID: hamcp-abc" in hint
        assert "not serving these yet" in hint

    def test_legacy_mode_pending_rotation_carries_restart_caveat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Review finding on #1880: after a rotation, entry.data updates
        # immediately but the bound views keep the previous identity until
        # restart -- the hint must say the shown values do not work yet.
        monkeypatch.setattr(cf, "_legacy_credentials_active", lambda *a: False)
        flow = _make_options_flow(
            options={const.OPT_WEBHOOK_AUTH: const.WEBHOOK_AUTH_LEGACY},
            data={
                const.DATA_OAUTH_CLIENT_ID: "hamcp-new",
                const.DATA_OAUTH_CLIENT_SECRET: "new-secret",
            },
        )
        hint = flow._oauth_creds_hint(cf._COMMON_FALLBACKS)
        assert "Client ID: hamcp-new" in hint
        assert "Client Secret: new-secret" in hint
        assert "not serving these yet" in hint
        assert "restart" in hint


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, "ha-mcp-dev==9.0.0.dev3"),
        ({const.OPT_PIP_SPEC: "acme-ha-mcp==1.0"}, "acme-ha-mcp==1.0"),
        (
            {
                const.OPT_RUNTIME_MODE: const.RUNTIME_NATIVE,
                const.OPT_PIP_SPEC: "acme-ha-mcp==1.0",
            },
            "ha-mcp-dev==9.0.0.dev3",
        ),
    ],
    ids=["pre-release-pin", "override", "native-ignores-legacy-override"],
)
def test_the_version_line_reads_what_the_server_runs(monkeypatch, options, expected):
    # #2787: the form reads the version as bring-up does, so an ha-mcp left
    # beside the pre-release pin or an override cannot mask it.
    from unittest.mock import MagicMock

    flow = _make_options_flow(options=options, data={const.DATA_WEBHOOK_ID: "mcp_abc"})
    flow.hass = MagicMock()
    flow.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    integration = SimpleNamespace(requirements=["ha-mcp-dev==9.0.0.dev3"], version="9")
    monkeypatch.setattr(
        cf, "async_get_integration", AsyncMock(return_value=integration)
    )
    read: list[str] = []
    monkeypatch.setattr(cf, "_installed_server_version", read.append)
    asyncio.run(flow.async_step_init(None))
    assert read == [expected]
