"""Component route for ``ha_search``.

Request building, dashboard-window merging and response shaping for the
``ha_mcp_tools`` search command, plus the methods that call it.
"""

import asyncio
import logging
from typing import Any

from ha_mcp._vendor.fastmcp import Context

from ...client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ...client.websocket_client import get_websocket_client
from ...runtime.search_component_results import (
    _COMPONENT_BODY_SEARCH_TYPES,
    _DASHBOARD_SEARCH_TYPE,
    _build_component_search_request,
    _component_body_search_types,
    _component_max_results,
    _component_serves_search_types,
    _dashboard_split_requested,
    _DashboardLeg,
    _merge_dashboard_window,
    _shape_component_search_response,
)
from ...visibility.model import VisibilityWire, wire_has_allowlist_dimensions
from ...visibility.resolver import (
    visibility_state_and_wire,
)
from ..component_api import (
    component_supports,
    invalidate_caps,
    is_unknown_command,
)
from ..smart_search import DEFAULT_CONCURRENCY_LIMIT
from .legacy import LegacySearchMixin
from .response import (
    _CONFIG_BUCKETS,
    _ResolvedSearch,
)

logger = logging.getLogger(__name__)


# Body surfaces the ``ha_mcp_tools`` component's ``search`` command accepts
# (its voluptuous allowlist also has ``entity``, appended separately by
# ``_build_component_search_request``). ``dashboard`` is deliberately absent:
# the command has no dashboard scanner and its ``vol.In(ALL_SEARCH_TYPES)``
# rejects the value, which bounced every such call off the component schema
# into a warning-laden fallback (issue #2008). The value is still ROUTE-
# eligible: a request naming it keeps the fast path for the surfaces above
# while the dashboards leg fills that bucket and the server merges the two
# (issue #2289) — only the wire request drops it.


# The ``search_types`` value the component's ``search`` command cannot serve;
# the dashboards leg serves it instead (issue #2289).


# The component's ``limits.max_results``, which its ``search`` schema enforces
# as a hard ``vol.Range(max=...)`` on ``limit``. The dashboard merge fetches a
# ``[0, offset + limit)`` window, so a deep enough page exceeds it — see
# ``_dashboard_split_serviceable``.


# Config buckets a component ``search`` result can carry. ``dashboards`` is
# excluded: the command has no dashboard surface, so that bucket always comes
# from the dashboards leg (issue #2289).


def _dashboard_split_serviceable(req: _ResolvedSearch, caps: Any) -> bool:
    """True when the split can serve the request; False ⇒ legacy serves it whole.

    Two corner cases route away rather than being half-served:

    - Nothing is left for the component command once ``dashboard`` is stripped.
      An explicit ``search_types`` pin also drops the entity surface, so
      ``search_types=["dashboard"]`` would send an empty request and leave the
      split as a dashboards leg wearing a component envelope — the legacy path
      already produces exactly that, with its own bucket.
    - On older components without ``search_unified``, the window fetch would
      ask for more records than the component's ``limit`` ceiling
      (``_component_max_results``), which its schema rejects outright.
    """
    serves_body = req.body_eligible and bool(_component_body_search_types(req))
    if not (req.registry_eligible or serves_body):
        return False
    return component_supports(caps, "search_unified") or (
        req.offset + req.limit <= _component_max_results(caps)
    )


async def _scrub_component_config_buckets(
    response: dict[str, Any], client: Any
) -> None:
    """Omit component config-body records referencing a hidden entity (enforce mode).

    The component's ``search_visibility`` wire applies the hide dimensions to
    ENTITY results only; its config-body records (automations/scripts/scenes/
    helpers/dashboards) can still reference a hidden entity, and the enforcement
    middleware's outbound scan would then refuse the whole search on contact
    instead of the issue-#2015 "collection reads omit" contract. Mirror of the
    legacy path's scrub (``_deep._scrub_results_for_enforce``), applied after
    ``_shape_component_search_response``. Totals are decremented by the dropped
    count — the component's corpus-side match count cannot be recomputed
    server-side. No-op unless enforce mode is active.
    """
    from ...visibility.enforcement import active_hidden_regex, scrub_records

    regex = await active_hidden_regex(client)
    if regex is None:
        return
    dropped = 0
    for bucket in _CONFIG_BUCKETS:
        records = response.get(bucket)
        if records:
            kept = scrub_records(records, regex)
            dropped += len(records) - len(kept)
            response[bucket] = kept
    if not dropped:
        return
    if isinstance(response.get("config_total_matches"), int):
        response["config_total_matches"] = max(
            0, response["config_total_matches"] - dropped
        )
    if isinstance(response.get("count"), int):
        response["count"] = max(0, response["count"] - dropped)


class ComponentSearchMixin(LegacySearchMixin):
    """Routes ``ha_search`` through the component search command."""

    async def _resolve_component_search_visibility(
        self, caps: Any
    ) -> tuple[bool, VisibilityWire | None]:
        """Decide the ha_search route under the entity-visibility gate.

        Returns ``(route_component, visibility_param)`` for a caller that has
        already confirmed the component advertises ``search``:

        - filter inactive → ``(True, None)``: the plain component search, no
          ``visibility`` param (parity with a pre-``search_visibility`` component).
        - filter active + ``search_visibility`` + config serialized:
          - component also advertises ``search_visibility_allowlist_authorization``
            → ``(True, <wire dict + allowlist_authorization: True>)``. The key opts
            the component into the revised rule (an allow match authorizes past
            category, HA-hidden, and Assist filters). It is never sent to a
            component lacking the capability: that component's strict schema would
            reject it, and the key's absence is what keeps a newer component on the
            legacy precedence the released server still applies in its own
            outbound scan.
          - no allowlist dimensions → ``(True, <wire dict>)``: the nine-key wire;
            with no allow dimensions the two precedences agree.
          - allowlist active without the capability → ``(False, None)``.
        - config could not be loaded → ``(False, None)``: the legacy path applies
          the filter server-side before the counts/pagination (fail-closed to
          legacy, matching ``visibility_state_and_wire``'s fail-closed pairing).

        ``visibility_state_and_wire`` loads the config once for both the active
        gate and its wire form, instead of the active check and the wire fetch
        each hitting the (memoized) config read separately.
        """
        active, visibility = await visibility_state_and_wire()
        if not active:
            return True, None
        if not component_supports(caps, "search_visibility") or visibility is None:
            return False, None
        if component_supports(caps, "search_visibility_allowlist_authorization"):
            authorized: VisibilityWire = {**visibility, "allowlist_authorization": True}
            return True, authorized
        if wire_has_allowlist_dimensions(visibility):
            return False, None
        return True, visibility

    async def _ha_search_via_component(
        self,
        req: _ResolvedSearch,
        ctx: Context | None,
        *,
        visibility: VisibilityWire | None = None,
        caps: Any = None,
    ) -> dict[str, Any] | None:
        """Serve ha_search from the component; ``None`` ⇒ run the legacy path.

        ``visibility`` is the serialized hide config passed to a
        ``search_visibility``-capable component so it applies the entity-
        visibility filter in-process (``None`` ⇒ no filter / not supported ⇒ the
        component surfaces every match, correct only because the caller routes
        here solely when the filter is inactive or the component can apply it).

        A ``search_types`` naming ``dashboard`` splits the work — the command
        serves the surfaces it has, the dashboards leg serves that bucket, and
        the two are merged (issue #2289). ``_dashboard_split_serviceable``
        names the corner cases that send the whole call to legacy instead;
        ``caps`` (the routing block's cached probe) feeds its
        component-advertised ``limits.max_results`` ceiling.
        """
        if _dashboard_split_requested(req):
            if not _dashboard_split_serviceable(req, caps):
                return None
            return await self._component_search_with_dashboards(
                req, ctx, visibility=visibility
            )
        try:
            raw = await self._send_component_search(req, visibility)
        except Exception as exc:  # noqa: BLE001
            return await self._component_search_fallback(req, ctx, exc)
        response = _shape_component_search_response(req, raw.get("result") or {})
        await _scrub_component_config_buckets(response, self._client)
        return response

    async def _component_search_fallback(
        self, req: _ResolvedSearch, ctx: Context | None, exc: Exception
    ) -> dict[str, Any] | None:
        """Apply the component-search failure taxonomy (design § 4).

        Returns the legacy-served response, or ``None`` when the caller should
        run the legacy path itself with no diagnostic:

        - ``unknown_command`` (component downgraded mid-session, so the cached
          positive caps are stale): invalidate the caps and return ``None`` so
          the caller falls back **silently** — an expected, non-actionable
          transition.
        - any other ``HomeAssistantCommandError`` (a component handler bug) or a
          ``HomeAssistantCommandTimeout`` (the component WS search timed out):
          serve the correct result from the legacy path, append a ``warnings[]``
          entry, and ``log.warning`` — correct results now, breakage visible.
        - ``HomeAssistantConnectionError`` - a pooled-WS drop, or a failed
          (re)connect: served the same way. The legacy path reads
          ``/api/states`` over REST and the entity registry through the
          ``send_websocket_message`` bridge, so a component-side fault degrades
          to partial results rather than escaping. The bridge shares this
          pooled connection, so a dead transport raises there too (#1947) and
          the registry-unavailable warning names what was skipped.
        """
        if isinstance(exc, (HomeAssistantCommandError, HomeAssistantCommandTimeout)):
            if is_unknown_command(exc):
                invalidate_caps(self._client)
                return None
            warning = f"component search path failed ({exc}); served via legacy path"
            log_message = "ha_mcp_tools/search failed; fell back to legacy: %r"
        else:
            warning = (
                f"component search connection error ({exc}); served via legacy path"
            )
            log_message = (
                "ha_mcp_tools/search connection error; fell back to legacy: %r"
            )
        legacy = await self._legacy_ha_search(req, ctx)
        legacy.setdefault("warnings", []).append(warning)
        logger.warning(log_message, exc)
        return legacy

    async def _component_search_with_dashboards(
        self,
        req: _ResolvedSearch,
        ctx: Context | None,
        *,
        visibility: VisibilityWire | None,
    ) -> dict[str, Any] | None:
        """Serve a ``dashboard``-including request from both legs (issue #2289).

        The two legs run concurrently: the dashboards leg is a second frame on
        the same pooled connection (or, on a component without
        ``dashboards_doc_search``, the legacy per-dashboard walk), so
        serialising them would add its latency to every mixed search.

        A failing component leg takes the taxonomy's legacy fallback, which
        serves the WHOLE call — dashboards bucket included — so the dashboards
        leg is CANCELLED on that failure rather than awaited: the legacy
        response carries its own dashboards bucket, and on the slow leg shapes
        (an older component's per-dashboard walk, fuzzy, ``include_config``)
        waiting would delay the fallback and then re-walk the same dashboards
        inside it.
        """
        component_task = asyncio.ensure_future(
            self._send_component_search(req, visibility, dashboard_split=True)
        )
        dashboard_task = asyncio.ensure_future(self._search_dashboards_leg(req))
        try:
            raw = await component_task
        except Exception as exc:  # noqa: BLE001
            # Settle the leg via ``asyncio.wait``, which never raises the
            # leg's own CancelledError — while a cancellation of THIS
            # coroutine delivered at that await still propagates instead of
            # being consumed right before the fallback runs the whole legacy
            # search uncancellably.
            dashboard_task.cancel()
            await asyncio.wait([dashboard_task])
            return await self._component_search_fallback(req, ctx, exc)
        except BaseException:
            # Cancellation / shutdown of THIS coroutine: settle the leg before
            # propagating, so no cancelled task outlives the call, then
            # re-raise like ``_legacy_ha_search``. A second cancellation
            # delivered during the wait propagates on its own, which is the
            # same outcome.
            dashboard_task.cancel()
            await asyncio.wait([dashboard_task])
            raise
        try:
            dashboard_outcome = await dashboard_task
        except BaseException:
            # Same invariant as above for the second await: a cancellation
            # delivered here would otherwise leave the leg task running
            # detached after this call unwinds. Settle it, then propagate.
            dashboard_task.cancel()
            await asyncio.wait([dashboard_task])
            raise

        windowed, dashboards_page = _merge_dashboard_window(
            req, raw.get("result") or {}, dashboard_outcome.records
        )
        response = _shape_component_search_response(
            req,
            windowed,
            dashboard_leg=_DashboardLeg(
                records=dashboards_page,
                failed=dashboard_outcome.failed,
                error=dashboard_outcome.error,
            ),
        )
        await _scrub_component_config_buckets(response, self._client)
        return response

    async def _search_dashboards_leg(self, req: _ResolvedSearch) -> _DashboardLeg:
        """The dashboards bucket for a merged component search.

        Reuses the deep path's own dashboards surface, which is already
        component-first (the ``dashboards_doc_search`` frame) with the legacy
        per-dashboard walk as its fallback — so the merged route covers exactly
        the dashboards the legacy route covers.

        An ordinary failure is reported through ``_DashboardLeg.error`` instead
        of raising: the other surfaces succeeded, so the call returns them and
        says the dashboard bucket is incomplete. Cancellation still propagates.
        """
        semaphore = asyncio.Semaphore(DEFAULT_CONCURRENCY_LIMIT)
        try:
            records, failed = await self._smart_tools._search_dashboards_surface(
                req.query_text.lower(),
                req.exact_match,
                semaphore,
                include_config=req.include_config,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ha_search dashboards leg failed: %r", exc)
            # ``str(asyncio.TimeoutError())`` is "" — fall back to the type name
            # so the errors[] entry never reads "dashboards: ".
            return _DashboardLeg(records=[], error=str(exc) or type(exc).__name__)
        return _DashboardLeg(records=list(records), failed=failed)

    async def _send_component_search(
        self,
        req: _ResolvedSearch,
        visibility: VisibilityWire | None = None,
        *,
        dashboard_split: bool = False,
    ) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/search`` command over the per-client WebSocket.

        ``visibility`` (the serialized hide config) is attached only when set, so
        a plain-``search`` component (which lacks the param in its schema) never
        receives it. ``dashboard_split`` asks for the window fetch the dashboard
        merge pages server-side.
        """
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        request = _build_component_search_request(req, dashboard_split=dashboard_split)
        if visibility is not None:
            request["visibility"] = visibility
        return await ws.send_command("ha_mcp_tools/search", **request)


__all__ = [
    "ComponentSearchMixin",
    "_COMPONENT_BODY_SEARCH_TYPES",
    "_DASHBOARD_SEARCH_TYPE",
    "_component_serves_search_types",
]
