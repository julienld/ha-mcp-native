"""The public frontend theme contract, independent of connection or Core owners."""

from typing import Annotated, Any, Literal, Protocol

from pydantic import Field

from ha_mcp.tools.coercion import JSON_STRING_COERCION

ThemeAction = Literal["list", "set", "get_engine_theme", "set_engine_theme"]


class ThemeService(Protocol):
    async def manage(
        self,
        action: ThemeAction,
        theme_name: str | None = None,
        expected_current: dict[str, Any] | None = None,
        force: bool = False,
        value: dict[str, Any] | None = None,
        mode: Literal["light", "dark"] | None = None,
    ) -> dict[str, Any]: ...


class ThemeTools:
    def __init__(self, themes: ThemeService) -> None:
        self.themes = themes

    async def ha_manage_theme(
        self,
        action: Annotated[
            ThemeAction,
            Field(
                description=(
                    "Theme operation: 'list' installed themes, 'set' the "
                    "backend default theme, or read/restore the screenshot "
                    "engine account's own per-user theme with "
                    "'get_engine_theme' / 'set_engine_theme'."
                ),
            ),
        ],
        theme_name: Annotated[
            str | None,
            Field(
                description=(
                    "Theme name when action='set'. Must be an installed theme; "
                    "'default' restores the built-in theme, 'none' resets the "
                    "chosen mode to the built-in default."
                ),
                default=None,
            ),
        ] = None,
        expected_current: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Guard for action='set_engine_theme': the stored theme is read "
                    "immediately before the write and the write is skipped if it no longer "
                    "equals this. Omitting this value or passing null both mean 'expect no "
                    "stored theme', enforced like any other value; the guard is always "
                    "applied unless force is set. Best-effort, not atomic: a change landing"
                    " between that read and the write is not caught."
                ),
                default=None,
            ),
        ] = None,
        force: Annotated[
            bool,
            Field(
                description=(
                    "action='set_engine_theme' only: skip the expected_current guard and "
                    "overwrite unconditionally."
                ),
                default=False,
            ),
        ] = False,
        value: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Frontend user-data theme object when "
                    "action='set_engine_theme', e.g. {'theme': '', "
                    "'dark': False}. An empty dict restores default/auto "
                    "behavior."
                ),
                default=None,
            ),
        ] = None,
        mode: Annotated[
            Literal["light", "dark"] | None,
            Field(
                description=(
                    "Which mode the theme applies to when action='set'. "
                    "Defaults to light."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage Home Assistant frontend themes.

        When NOT to use: themes are YAML files - Home Assistant has no API to
        create or edit them. Installing community themes goes through HACS
        (ha_manage_hacs); editing custom theme files goes through
        ha_config_set_yaml (beta, edits themes/<name>.yaml keyed by theme name
        and attempts an automatic theme reload).

        When to use: action='list' discovers installed theme names and the
        current defaults; action='set' selects the backend default theme
        (optionally per light/dark mode).

        SCREENSHOT-ENGINE ACTIONS (per-user, not the backend default):
        Taking a dashboard screenshot makes the Puppet engine write the saved
        theme of the Home Assistant user its token belongs to, which also
        flips that user's live web and mobile sessions. The screenshot tools
        are read-only and only *report* this; use action='set_engine_theme'
        with the value quoted in their warning to put it back, and
        action='get_engine_theme' to inspect it. These act on that engine
        account's per-user profile via frontend/set_user_data, which is a
        different layer from the backend default that action='set' changes.
        Giving the engine its own dedicated user and token avoids the issue
        entirely.

        Caveats: action='set' changes the backend-selected default only -
        users who explicitly picked a theme in their profile keep their
        choice. Theme names are validated by Home Assistant at call time.

        EXAMPLES:
        - List themes: ha_manage_theme(action="list")
        - Set default theme: ha_manage_theme(action="set", theme_name="nord")
        - Set dark-mode theme: ha_manage_theme(
              action="set", theme_name="nord", mode="dark")
        - Restore built-in default: ha_manage_theme(
              action="set", theme_name="default")
        - Inspect the engine account's theme: ha_manage_theme(
              action="get_engine_theme")
        - Undo a screenshot's theme change (pass BOTH values from the
          warning, so a theme changed since then is not overwritten):
          ha_manage_theme(action="set_engine_theme",
              value={"theme": "", "dark": False},
              expected_current={"theme": "default", "dark": True})
        """
        return await self.themes.manage(
            action=action,
            theme_name=theme_name,
            expected_current=expected_current,
            force=force,
            value=value,
            mode=mode,
        )
