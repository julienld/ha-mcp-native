"""Home Assistant reference-graph (``search/related``) lookups for deep search.

Discussion #2258. The legacy config-body path has two blind spots that no
budget can close:

- **Budget-skipped configs.** HA's ``/config/<domain>/config/<id>`` endpoint
  takes a mutation lock and re-parses the whole YAML file per request, so the
  reads are serialized server-side. On a large install the wall-clock budget
  expires with an arbitrary slice of automations never fetched, and an
  unfetched config scores 0 and is dropped silently.
- **YAML-defined configs.** That same endpoint only exposes UI-storage items,
  so a YAML automation returns 404 at *any* budget and can never be scanned.

For an entity_id-shaped query, HA answers "what references this entity" from
its own in-memory graph in a single WebSocket frame, covering both cases. It is
not a replacement for the body scan: HA's reference extractor skips templated
entity ids (``helpers/script.py`` bails on a ``Template`` instance), so
``{{ states('climate.x') }}`` is invisible to the graph and visible only to the
body scan. The two are merged, never substituted.

This module owns the query-shape gate, the frame, and the response parsing. The
merge into the result buckets lives in ``_deep`` next to the buckets it mutates.
"""

from __future__ import annotations

import logging
import re
import time
import weakref
from dataclasses import dataclass
from typing import Any

from ...errors import get_error_code, get_error_message
from ...runtime.search_references import (
    GRAPH_ITEM_TYPES,
    GraphResult,
    _graph_partial_reasons,
    _merge_graph_hits,
    _parse_related,
)
from ..component_api import UNKNOWN_COMMAND_CODE

logger = logging.getLogger(__name__)

# HA's own entity-id grammar, mirrored from ``homeassistant/core.py`` rather
# than imported (this server does not depend on the homeassistant package).
# Both halves are slugs of ``[a-z0-9_]`` that may not start or end with an
# underscore. The ``(?!.+__)`` guard reads as a domain rule but is anchored at
# the start of the whole pattern and ``.`` matches the separator, so upstream
# it rejects a double underscore ANYWHERE in the entity id, object_id
# included. Copied verbatim so this gate can never be stricter than HA's own
# validator.
_OBJECT_ID = r"(?!_)[\da-z_]+(?<!_)"
_DOMAIN = r"(?!.+__)" + _OBJECT_ID
_VALID_ENTITY_ID = re.compile(r"^" + _DOMAIN + r"\." + _OBJECT_ID + r"$")

# ``search/related`` item types that map onto a config-body bucket.
#
# HA also returns ``group`` / ``person`` consumers, and the "up" relations an
# entity belongs to (``area`` / ``device`` / ``floor`` / ``label`` /
# ``config_entry``). Neither class is merged: ha_search has no group or person
# bucket, and the "up" relations answer "what does this entity belong to",
# not "what breaks if I rename it". Dropping them explicitly (rather than
# mapping whatever arrives) keeps an added HA item type from silently landing
# in a bucket whose record shape it does not fit.

# Consumer item types HA reports for an entity that this server does not merge.
# These break on a rename exactly like an automation does, so they are counted
# and named in ``partial_reason`` rather than dropped in silence: a response
# that discards them AND claims the reference list is complete is the worst
# possible answer for a "is this safe to rename" question.

# "Up" relations: what the entity BELONGS to, not what would break if it were
# renamed. Ignored silently because they are not references at all.

# Every item type this code recognises. A payload naming none of them is a
# shape we cannot read (see ``_parse_related``), which is the opposite of
# "Home Assistant knows of no references".

# Bucket name back to the item type it came from. Callers filtering buckets
# against ``search_types`` need the singular; deriving it here beats
# reconstructing it with ``bucket[:-1]`` string surgery, which silently
# mis-classifies the first bucket whose name is not ``singular + "s"``.


# How long a definitive "this HA has no search/related" verdict suppresses
# further attempts, per client. The ``search`` integration ships in
# ``default_config`` so this is near-universal, but an install without it
# would otherwise pay a rejected frame on every entity_id-shaped search, and
# ``send_websocket_message`` logs each rejection at ERROR — the log-spam
# regression of issue #1889. Negative only: a successful graph must be
# re-read every search, since it changes whenever HA reloads a config.
# The graph runs ahead of the config scan it exists to improve, so it gets a
# short leash rather than send_command's 30s default.
_GRAPH_TIMEOUT_S = 5.0

_UNSUPPORTED_TTL_S = 300.0
# Suppression needs the rejection twice. HA registers the ``search`` command
# during setup and accepts WebSocket clients BEFORE every integration has
# loaded, so a single ``unknown_command`` is as likely to mean "HA is still
# starting" as "this install has no search integration" -- and the most
# safety-critical moment to consult the graph is right after ``ha_restart``.
_UNSUPPORTED_STRIKES_REQUIRED = 2


def _monotonic() -> float:
    """Monotonic clock read, isolated so tests can advance it deterministically.

    Mirrors ``component_api._monotonic``. The seam matters because ``_graph.time``
    IS the stdlib ``time`` module, so patching ``monotonic`` through it would
    replace the clock for every module in the process, not just this one.
    """
    return time.monotonic()


@dataclass
class _Unsupported:
    """Per-client rejection history. One object so the two fields cannot drift.

    ``since`` is set only once ``strikes`` reaches the threshold; until then a
    rejection is provisional and the graph is still consulted.
    """

    strikes: int = 0
    since: float | None = None


_UNSUPPORTED: weakref.WeakKeyDictionary[Any, _Unsupported] = weakref.WeakKeyDictionary()


def is_entity_id_shaped(value: str) -> bool:
    """True when ``value`` is a syntactically valid ``domain.object_id``.

    Deliberately syntactic, with no registry lookup: a YAML template sensor
    declared without a ``unique_id`` has no registry entry yet is referenced by
    automations like any other entity, and gating on registry membership would
    skip exactly those. A shaped query that matches nothing simply costs one
    frame that returns an empty graph.
    """
    if not isinstance(value, str):
        return False
    return _VALID_ENTITY_ID.match(value) is not None


def _unsupported_recently(client: Any) -> bool:
    """True while a confirmed ``search/related`` rejection is still cached.

    Logs at WARNING each time it suppresses. Silence here would make "why did
    ``match_in_references`` stop appearing" undiagnosable from a log: the one
    rejection that armed the window may be minutes old and every search since
    would have left no trace.
    """
    state = _UNSUPPORTED.get(client)
    since = state.since if state else None
    if since is None:
        return False
    if (_monotonic() - since) < _UNSUPPORTED_TTL_S:
        logger.warning(
            "Skipping Home Assistant's reference graph: this instance rejected "
            "search/related as an unknown command. Results come from the "
            "configuration-body scan alone. Retrying in at most %.0fs.",
            _UNSUPPORTED_TTL_S - (_monotonic() - since),
        )
        return True
    _UNSUPPORTED.pop(client, None)
    return False


def reset_unsupported_cache(client: Any) -> None:
    """Forget a client's rejection history.

    The seam the suppression tests use to isolate module-level state, since
    the ``_UNSUPPORTED`` map outlives any one test.
    Nothing in production calls it: the cache is keyed by the long-lived REST
    client, which outlives reconnects and HA restarts, so the TTL is the only
    automatic recovery an install gets. That is deliberate rather than
    overlooked, and it is why the TTL is minutes and not hours.
    """
    _UNSUPPORTED.pop(client, None)


def _looks_unsupported(response: dict[str, Any]) -> bool:
    """True when an error envelope means HA does not serve this command.

    Reads the structured code first so a reworded message upstream cannot
    silently turn this check off, and accepts both envelope shapes the way
    ``_deep._is_no_stored_config`` does: ``send_websocket_message`` flattens a
    command failure (code as ``error_code``), while ``get_error_code`` handles
    the nested ``error.code`` form. The text match stays as a last resort for
    envelopes carrying no code at all.
    """
    if UNKNOWN_COMMAND_CODE in (response.get("error_code"), get_error_code(response)):
        return True
    text = (get_error_message(response) or "").lower()
    return "unknown command" in text or "unknown_command" in text


async def fetch_related_buckets(client: Any, entity_id: str) -> GraphResult | None:
    """Ask HA which automations/scripts/scenes reference ``entity_id``.

    Returns a :class:`GraphResult` on success (possibly empty: HA knows of no
    references), or ``None`` when the graph could not be consulted OR its answer
    could not be understood. The distinction matters for the partial message —
    only a successful, readable call licenses any claim about reference
    completeness.

    Never raises. Every failure mode degrades to ``None`` and leaves the
    config-body scan to answer alone, exactly as before this existed.
    """
    if _unsupported_recently(client):
        return None
    try:
        response = await client.send_websocket_message(
            {
                "type": "search/related",
                "item_type": "entity",
                "item_id": entity_id,
                # The graph is an optimisation consulted BEFORE the config
                # scan, so inheriting send_command's 30s default would stall
                # every search behind an unresponsive optional lookup. Popped
                # by the client; never reaches Home Assistant.
                "_wait_timeout": _GRAPH_TIMEOUT_S,
            }
        )
    except Exception as e:  # noqa: BLE001
        # Transport-class failure: ``send_websocket_message`` raises only when
        # the socket itself could not carry the frame. Not cached — the next
        # search may well reconnect.
        logger.debug(f"search/related transport failure for {entity_id}: {e}")
        return None

    if not isinstance(response, dict):
        return None
    if response.get("success") is False:
        error = response.get("error")
        if _looks_unsupported(response):
            # Arm suppression only on a REPEAT rejection, so a command rejected
            # while HA was still loading integrations does not disable the
            # graph for the whole window. Once armed it stops the ERROR line
            # send_websocket_message emits per call (#1889).
            state = _UNSUPPORTED.setdefault(client, _Unsupported())
            state.strikes += 1
            strikes = state.strikes
            if strikes >= _UNSUPPORTED_STRIKES_REQUIRED:
                state.since = _monotonic()
                logger.warning(
                    "Home Assistant rejected search/related %d times; its "
                    "`search` integration appears not to be loaded. Pausing "
                    "reference-graph lookups for %.0fs.",
                    strikes,
                    _UNSUPPORTED_TTL_S,
                )
            else:
                logger.debug(
                    "search/related rejected as unknown (strike %d/%d); "
                    "not suppressing yet in case HA is still starting",
                    strikes,
                    _UNSUPPORTED_STRIKES_REQUIRED,
                )
        else:
            logger.debug(f"search/related failed for {entity_id}: {error}")
        return None

    _UNSUPPORTED.pop(client, None)
    parsed = _parse_related(response.get("result"))
    if parsed is None:
        logger.debug(
            "search/related answered for %s in a shape this code cannot read; "
            "treating as not consulted rather than as 'no references'",
            entity_id,
        )
    return parsed


# Score assigned to a reference-graph hit. In the default exact mode every
# surviving match scores exactly 100 (``_score_deep_match`` takes the max of
# two 0-or-100 signals against a threshold of 100), so a graph hit sorts
# indistinguishably from a body hit.
#
# Fuzzy mode is not symmetric, and no value here makes it so without breaking
# the tie above: the 100 cap belongs to the config-body score
# (``_search_in_dict``), while the NAME score from ``_calculate_entity_score``
# is not capped at 100 — a name echoing the queried entity's words can pass
# 100 on its own. So with ``exact_match=False`` a graph-ONLY hit
# (``_merge_graph_hits`` leaves an already-scored record's score alone) can sort
# below a match that merely resembles the query, and land on a later page under
# a small ``limit``. It is still counted in ``total_matches`` unless visibility
# enforcement scrubs it.


def _graph_applies(query_lower: str, search_types: list[str]) -> bool:
    """True when Home Assistant's reference graph could answer this query.

    Two independent conditions, and BOTH must be checked wherever the answer
    matters. The graph takes an item id, so a free-text term is a guaranteed
    miss; and it speaks only to automations, scripts and scenes, so a call
    requesting none of those has nothing to gain from a frame.

    Kept as one predicate because the caller needs the same verdict twice: to
    decide whether to ask, and to decide whether NOT asking was a loss worth
    disclosing. Deriving the second from half of the first reported a complete
    helper-only search as ``partial`` on the strength of a frame that was never
    warranted.
    """
    if not is_entity_id_shaped(query_lower):
        return False
    return any(item_type in search_types for item_type in GRAPH_ITEM_TYPES)


__all__ = [
    "_graph_partial_reasons",
    "_merge_graph_hits",
    "fetch_related_buckets",
    "is_entity_id_shaped",
    "reset_unsupported_cache",
]
