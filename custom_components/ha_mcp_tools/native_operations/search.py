"""Canonical native Search uses Core owners and filters before counting."""

from dataclasses import asdict, dataclass, field
from functools import partial
from typing import Any

from homeassistant.helpers.json import json_bytes

from ha_mcp.runtime.progress import report_progress
from ha_mcp.runtime.search_component_results import (
    _build_component_search_request,
    _DashboardLeg,
)
from ha_mcp.runtime.search_references import (
    GraphResult,
    _parse_related,
)
from ha_mcp.runtime.search_request import resolve_search_request
from ha_mcp.runtime.search_response import _ResolvedSearch
from ha_mcp.tool_api import CallContext, ToolFailure
from ha_mcp.utils.owned_executor import await_owned_io

from ..native_visibility import hidden_from_core
from ..websocket_api.search import _do_search, _search_prep
from .registry_access import authorize_actor
from .search_dashboards import load_dashboard_documents
from .search_projection import project_search
from .search_sources import (
    ConfigBodies,
    SearchCandidates,
    load_config_bodies,
    search_setting,
)


@dataclass(slots=True)
class NativeSearch:
    manager: Any
    actor: CallContext
    config: Any = field(default=None, init=False)
    threshold: int = field(default=60, init=False)
    hidden: frozenset[str] = field(default_factory=frozenset, init=False)

    async def _boundary(self, *, initial: bool = False) -> None:
        config = await self.manager.visibility._config("ha_search")
        await authorize_actor(self.actor, self.manager.backend.authorize)
        hidden = (
            frozenset(hidden_from_core(self.manager.hass, config))
            if config is not None
            else frozenset()
        )
        if initial:
            self.config = config
            self.hidden = hidden
        elif config != self.config or hidden != self.hidden:
            raise ToolFailure(
                "CONFIG_CONFLICT", "Entity visibility changed during search."
            )

    def _related(self, req: _ResolvedSearch) -> tuple[bool, GraphResult | None]:
        from homeassistant.core import valid_entity_id

        if (
            not req.body_eligible
            or not {"automation", "script", "scene"}.intersection(
                req.parsed_search_types or ("automation", "script", "scene", "helper")
            )
            or not valid_entity_id(req.query_text.lower())
        ):
            return False, None
        try:
            from homeassistant.components.search import ItemType, Searcher
            from homeassistant.helpers.entity import entity_sources

            # Core 10's optional disabled-entity flag defaults to the Core 8/9 behavior.
            payload = Searcher(
                self.manager.hass,
                entity_sources(self.manager.hass),
            ).async_search(ItemType.ENTITY, req.query_text.lower())
            return True, _parse_related(payload)
        except Exception:  # noqa: BLE001 - disclose unavailable graph; never acquire through another transport
            return True, None

    def _capture(
        self,
        req: _ResolvedSearch,
        params: dict[str, Any],
        extra: dict[str, Any],
        stored: ConfigBodies,
        dashboards: _DashboardLeg,
    ) -> bytes:
        candidates = SearchCandidates()
        degraded = extra.get("secret_scrub_degraded", False)
        # Capture every candidate and Core join. Query scoring belongs only to
        # the detached worker, not to this loop-owned acquisition phase.
        raw = _do_search(
            self.manager.hass,
            {**params, "query": None},
            **{**extra, "secret_scrub_degraded": False},
            stored_configs=stored.stored,
            record_collector=candidates.collect,
        )
        candidates.include_persisted(stored)
        consulted, related = (False, None) if degraded else self._related(req)
        from homeassistant.helpers import entity_registry as er

        identities = {}
        if related is not None:
            registry = er.async_get(self.manager.hass)
            for ids in related.buckets.values():
                for eid in ids:
                    row = registry.async_get(eid)
                    if row is not None and row.unique_id:
                        identities[eid] = str(row.unique_id)
        detached: bytes = json_bytes(
            {
                "result": raw,
                "bodies": candidates.bodies if req.include_config else {},
                "body_omissions": candidates.omitted if req.include_config else [],
                "corpora": candidates.corpora,
                "entity_corpora": candidates.entities,
                "secret_values": list(extra.get("secret_values", ())),
                "hidden": sorted(self.hidden),
                "enforce": bool(self.config is not None and self.config.enforce),
                "graph_consulted": consulted,
                "graph": {
                    "buckets": {
                        bucket: sorted(ids) for bucket, ids in related.buckets.items()
                    },
                    "dropped": {
                        kind: sorted(ids) for kind, ids in related.dropped.items()
                    },
                }
                if related is not None
                else None,
                "graph_identities": identities,
                "graph_states": [
                    state.as_dict() for state in self.manager.hass.states.async_all()
                ]
                if related is not None
                else [],
                "source_reasons": stored.reasons,
                "dashboards": asdict(dashboards),
                "dashboards_requested": bool(
                    req.body_eligible
                    and req.parsed_search_types
                    and "dashboard" in req.parsed_search_types
                ),
            }
        )
        return detached

    async def search(self, **arguments: Any) -> dict[str, Any]:
        req = resolve_search_request(arguments)
        self.threshold = int(search_setting(self.manager, "fuzzy_threshold"))
        await self._boundary(initial=True)
        await report_progress(
            self.actor.progress,
            progress=0,
            total=3,
            message="Reading Core search sources",
        )
        params = _build_component_search_request(req)
        if not params["search_types"]:
            params["search_types"] = ["unavailable"]
        # The merged window needs all metadata, never all serialized bodies.
        params.update(limit=2**31 - 1, offset=0, include_config=False)
        if self.config is not None:
            params["visibility"] = {
                **self.config.to_wire(),
                "allowlist_authorization": True,
            }
        prep_params = params
        if req.body_eligible and "dashboard" in (req.parsed_search_types or []):
            # Dashboards share the config scrub even though their loader sits
            # beside the component scanner rather than inside its search types.
            prep_params = {
                **params,
                "search_types": [*params["search_types"], "automation"],
            }
        extra = await await_owned_io(
            _search_prep(self.manager.hass, prep_params, include_scrub_status=True)
        )
        await self._boundary()
        domains = list(params["search_types"]) if req.body_eligible else []
        degraded = extra.get("secret_scrub_degraded", False)
        if degraded:
            params["search_types"] = [
                surface for surface in params["search_types"] if surface == "entity"
            ] or ["unavailable"]
            stored = ConfigBodies(
                reasons=[
                    "Configuration search omitted because the secret scrub could not be loaded safely"
                ]
            )
        else:
            stored = await load_config_bodies(
                self.manager, req, domains, self._boundary
            )
        dashboards = _DashboardLeg([])
        if (
            req.body_eligible
            and "dashboard" in (req.parsed_search_types or [])
            and not degraded
        ):
            dashboards = await load_dashboard_documents(
                self.manager, self.actor, req, self._boundary
            )
        await self._boundary()
        snapshot = self._capture(req, params, extra, stored, dashboards)
        await report_progress(
            self.actor.progress,
            progress=2,
            total=3,
            message="Projecting search results",
        )
        result: dict[str, Any] = await await_owned_io(
            self.manager.hass.async_add_executor_job(
                partial(
                    project_search,
                    snapshot,
                    req,
                    self.threshold,
                )
            )
        )
        await report_progress(
            self.actor.progress, progress=3, total=3, message="Search complete"
        )
        await self._boundary()
        return result
