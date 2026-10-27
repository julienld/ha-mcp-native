"""Pure Lovelace card discovery and search result projection."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any

from ha_mcp.utils.config_hash import compute_config_hash

logger = logging.getLogger(__name__)


def _badge_matches(badge: Any, pattern: str) -> bool:
    """Whether ``badge``'s entity matches ``pattern`` (see ``_entity_matches``).

    Badges can be simple strings (entity IDs) or dicts with an 'entity' field.
    """
    return _entity_matches(
        pattern, badge.get("entity") if isinstance(badge, dict) else badge
    )


def _entity_matches(pattern: str, candidate: object) -> bool:
    """Whether ``candidate`` is a non-empty entity id matching ``pattern``.

    ``*`` in ``pattern`` matches any run of characters; every other character
    is literal, and the whole id must match.
    """
    if not isinstance(candidate, str) or not candidate:
        return False
    regex = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(regex, candidate) is not None


_NESTED_CARDS_KEY = "cards"


_NESTED_CARD_KEY = "card"


_NESTED_CUSTOM_FIELDS_KEY = "custom_fields"


_NESTED_STATES_KEY = "states"


_NON_CARD_CHILD_KEYS = ("elements",)


_BADGE_CARD_TYPE = "badge"


_MAX_CARD_DEPTH = 50


_MAX_NODE_DEPTH = 100


def _py_key(name: str) -> str:
    """Render a mapping key as a Python subscript segment (``['name']``).

    ``repr`` quotes and escapes the key, so a name containing a quote (e.g.
    ``o'brien``) yields a valid literal; a raw ``['{name}']`` interpolation would
    splice an unterminated string into ``python_transform``.
    """
    return f"[{name!r}]"


def _jq_key(name: str) -> str:
    """Render a mapping key as a jq path segment.

    Identifier-safe keys use dot notation (``.name``); any other key (a dot, a
    space, a quote) is emitted as a bracketed JSON string (``["weird.key"]``) so
    jq does not read an embedded dot as further nesting.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return f".{name}"
    return f"[{json.dumps(name)}]"


def _log_non_str_key(container_key: str, name: object, jq_prefix: str) -> None:
    """Breadcrumb a non-string mapping key under a card-bearing container.

    Dashboard config arrives as JSON, so keys are normally strings; a non-string
    key (from a corrupted or hand-edited config) cannot form a valid path, so the
    entry is skipped rather than crashing the walk via ``_jq_key`` / ``_py_key``.
    """
    logger.debug(
        "Card-search skipping non-string %s key at %s (%r, %s)",
        container_key,
        jq_prefix,
        name,
        type(name).__name__,
    )


@dataclass(frozen=True, slots=True)
class _SearchCriteria:
    """What a card search matches; every criterion that is set must hold.

    Values are trimmed and a blank one counts as not given.
    """

    entity_id: str | None = None
    card_type: str | None = None
    heading: str | None = None
    query: str | None = None

    def __post_init__(self) -> None:
        for criterion in fields(self):
            value = getattr(self, criterion.name)
            if isinstance(value, str):
                object.__setattr__(self, criterion.name, value.strip() or None)

    @property
    def query_lower(self) -> str | None:
        return self.query.lower() if self.query is not None else None

    def has_card_criteria(self) -> bool:
        """Whether a criterion that matches card fields (not text) is set."""
        return any(
            v is not None for v in (self.entity_id, self.card_type, self.heading)
        )

    def targets_cards(self) -> bool:
        """Whether a card-field criterion can select cards (not only badges)."""
        return self.has_card_criteria() and self.card_type != _BADGE_CARD_TYPE

    def can_match_cards(self) -> bool:
        """Whether cards can match at all (card_type='badge' selects only badges)."""
        return self.card_type != _BADGE_CARD_TYPE

    def can_match_badges(self) -> bool:
        """Whether view badges can match: they carry no heading, and any other
        card_type names a card."""
        return self.heading is None and self.card_type in (None, _BADGE_CARD_TYPE)

    def is_empty(self) -> bool:
        return self.query is None and not self.has_card_criteria()


@dataclass(frozen=True, slots=True)
class _SearchGaps:
    """Where a search could not look; each list feeds one response warning.

    Frozen only against field reassignment: the lists are shared collectors
    appended to in place.
    """

    truncation: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)

    def note_unread(self, value: Any, path: str) -> None:
        """Record container key ``path`` when present (not ``None``) in a shape
        the walk does not read.

        Such a list entry, ``None`` included, is recorded directly.
        """
        if value is not None:
            self.unread.append(path)

    def extend(self, other: _SearchGaps, prefix: str) -> None:
        self.truncation.extend(prefix + p for p in other.truncation)
        self.uncovered.extend(prefix + p for p in other.uncovered)
        self.unread.extend(prefix + p for p in other.unread)


@dataclass(frozen=True, slots=True)
class _CardWalkFrame:
    """Context ``_walk_card`` carries through every nested card.

    ``gaps`` is a shared collector every copy appends to.
    """

    criteria: _SearchCriteria
    view_index: int
    section_index: int | None
    card_index: int | None
    depth: int
    gaps: _SearchGaps

    def with_indices(
        self, *, section_index: int | None, card_index: int | None
    ) -> _CardWalkFrame:
        """The frame for another top-level card of the view, at the same depth."""
        return replace(self, section_index=section_index, card_index=card_index)

    def descend(self) -> _CardWalkFrame:
        """The frame for a card nested one level deeper."""
        return replace(self, depth=self.depth + 1)


@dataclass(slots=True)
class _CardSplit:
    """A card's own string leaves, the cards below it, and where descent stopped."""

    leaves: list[tuple[str, str]] = field(default_factory=list)
    cards: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    cut: list[str] = field(default_factory=list)


def _split_card_node(
    node: Any, path: tuple[str, str], key: str, out: _CardSplit, depth: int = 0
) -> None:
    """Split a card's subtree into its own leaves and the cards below it.

    ``path`` is the (jq, python) suffix of ``node``. Every dict and list is
    descended, down to ``_MAX_NODE_DEPTH`` levels (deeper subtrees are recorded
    in ``out.cut``); a typed dict in a card slot goes to ``out.cards`` with its
    paths instead of contributing leaves. Leaves are strings (scalars as ``str()``)
    and carry their nearest dict key
    (a ``custom_fields`` / ``states`` value carries its name).
    """
    jq, py = path
    if isinstance(node, str):
        if node:
            out.leaves.append((key, node))
        return
    if isinstance(node, (bool, int, float)):
        out.leaves.append((key, str(node)))
        return
    if not isinstance(node, (list, dict)):
        return
    if depth > _MAX_NODE_DEPTH:
        out.cut.append(jq)
        return
    if isinstance(node, list):
        for i, item in enumerate(node):
            _split_card_node(item, (f"{jq}[{i}]", f"{py}[{i}]"), key, out, depth + 1)
        return
    for k, v in node.items():
        if isinstance(k, str):
            _split_card_entry(k, v, path, out, depth + 1)
        else:
            _log_non_str_key("card", k, jq)


def _split_card_entry(
    key: str, value: Any, path: tuple[str, str], out: _CardSplit, depth: int
) -> None:
    """``_split_card_node`` for one ``key: value`` entry of a dict at ``path``."""
    child_jq, child_py = f"{path[0]}{_jq_key(key)}", f"{path[1]}{_py_key(key)}"
    slots = _card_slots(key, value, child_jq)
    if slots is None:
        _split_card_node(value, (child_jq, child_py), key, out, depth)
        return
    for jq_seg, py_seg, leaf_key, item in slots:
        item_path = (f"{child_jq}{jq_seg}", f"{child_py}{py_seg}")
        if isinstance(item, dict) and "type" in item:
            out.cards.append((*item_path, item))
        else:
            _split_card_node(item, item_path, leaf_key, out, depth)


def _card_slots(
    key: str, value: Any, jq: str
) -> list[tuple[str, str, str, Any]] | None:
    """``(jq segment, python segment, leaf key, item)`` per slot.

    ``None`` when ``key: value`` holds no card slots.
    """
    if key in (_NESTED_CARDS_KEY, _NESTED_CARD_KEY) and isinstance(value, list):
        return [(f"[{i}]", f"[{i}]", key, item) for i, item in enumerate(value)]
    if key == _NESTED_CARD_KEY and isinstance(value, dict):
        return [("", "", key, value)]
    if key in (_NESTED_CUSTOM_FIELDS_KEY, _NESTED_STATES_KEY) and isinstance(
        value, dict
    ):
        slots: list[tuple[str, str, str, Any]] = []
        for name, item in value.items():
            if isinstance(name, str):
                slots.append((_jq_key(name), _py_key(name), name, item))
            else:
                _log_non_str_key(key, name, jq)
        return slots
    return None


def _walk_card(
    card: Any,
    *,
    jq_prefix: str,
    python_prefix: str,
    frame: _CardWalkFrame,
) -> list[dict[str, Any]]:
    """Return matches for ``card`` and every card nested beneath it.

    ``jq_prefix`` / ``python_prefix`` locate ``card`` itself — the former in jq
    dot-notation, the latter as a Python subscript chain usable (appended after
    ``config``) directly in ``ha_config_set_dashboard(python_transform=...)``.
    Nested cards (every card slot at any depth, see ``_split_card_node``)
    extend both prefixes, so the paths are the authoritative locator; the flat
    ``view_index`` / ``section_index`` / ``card_index`` identify the top-level
    container only.

    Only a dict carrying a ``type`` key is matched as a card. Subtrees past
    ``_MAX_CARD_DEPTH`` card levels are recorded in ``frame.gaps.truncation``;
    cards carrying ``_NON_CARD_CHILD_KEYS`` in ``frame.gaps.uncovered``.
    """
    if not isinstance(card, dict):
        if frame.criteria.can_match_cards():
            frame.gaps.unread.append(jq_prefix)
        return []
    if frame.depth > _MAX_CARD_DEPTH:
        logger.warning(
            "Card-search depth bound (%d) exceeded at %s; not descending further",
            _MAX_CARD_DEPTH,
            jq_prefix,
        )
        frame.gaps.truncation.append(jq_prefix)
        return []

    split = _CardSplit()
    _split_card_node(card, ("", ""), "", split)
    frame.gaps.truncation.extend(jq_prefix + cut for cut in split.cut)
    matches: list[dict[str, Any]] = []
    if "type" in card:
        match = _card_search_match(card, split.leaves, jq_prefix, python_prefix, frame)
        if match is not None:
            matches.append(match)
        _note_uncovered(card, jq_prefix, frame)
    elif frame.criteria.can_match_cards():
        # Only top-level and header entries reach here untyped (nested ones are
        # leaves of their parent); the frontend renders them as error cards.
        frame.gaps.unread.append(jq_prefix)
    child_frame = frame.descend()
    for jq_suffix, py_suffix, child in split.cards:
        matches.extend(
            _walk_card(
                child,
                jq_prefix=f"{jq_prefix}{jq_suffix}",
                python_prefix=f"{python_prefix}{py_suffix}",
                frame=child_frame,
            )
        )
    return matches


def _note_uncovered(
    card: dict[str, Any], jq_prefix: str, frame: _CardWalkFrame
) -> None:
    for key in _NON_CARD_CHILD_KEYS:
        if card.get(key):
            frame.gaps.uncovered.append(f"{jq_prefix}.{key}")
            return


def _card_search_match(
    card: dict[str, Any],
    leaves: list[tuple[str, str]],
    jq_prefix: str,
    python_prefix: str,
    frame: _CardWalkFrame,
) -> dict[str, Any] | None:
    """The match record for ``card`` when it meets every criterion in ``frame``."""
    criteria = frame.criteria
    hits = _query_hits(leaves, criteria.query_lower)
    if hits == [] or not _card_matches(card, criteria):
        return None
    match: dict[str, Any] = {
        "view_index": frame.view_index,
        "section_index": frame.section_index,
        "card_index": frame.card_index,
        "jq_path": jq_prefix,
        "python_path": python_prefix,
        "card_type": card.get("type"),
        "card_config": card,
    }
    if hits is not None:
        match["matched"] = hits
    return match


def _query_hits(
    leaves: list[tuple[str, str]], query_lower: str | None
) -> list[dict[str, str]] | None:
    """``{field, value}`` per leaf containing the lower-cased query; ``None`` without one."""
    if query_lower is None:
        return None
    return [{"field": f, "value": v} for f, v in leaves if query_lower in v.lower()]


def _split_badge(badge: Any, out: _CardSplit) -> None:
    """``_split_card_node`` for a badge, folding any card nested in it into its leaves.

    A badge is one match, so text under a card it holds is the badge's own.
    """
    _split_card_node(badge, ("", ""), "badges", out)
    while out.cards:
        jq, py, card = out.cards.pop()
        _split_card_node(card, (jq, py), "", out)


def _find_badge_matches_in_view(
    view: dict[str, Any], frame: _CardWalkFrame
) -> list[dict[str, Any]]:
    """View-level badges matching ``frame``'s criteria.

    Badges are entity references, so they answer entity_id and text searches,
    and ``card_type='badge'`` alone lists every well-formed badge (an entity id
    or a mapping); entries in any other shape are recorded in ``frame.gaps``.
    """
    view_idx = frame.view_index
    criteria = frame.criteria
    badges = view.get("badges")
    if not criteria.can_match_badges():
        return []
    if not isinstance(badges, list):
        frame.gaps.note_unread(badges, f".views[{view_idx}].badges")
        return []
    matches: list[dict[str, Any]] = []
    for badge_idx, badge in enumerate(badges):
        is_dict_badge = isinstance(badge, dict)
        is_entity_badge = isinstance(badge, str) and bool(badge.strip())
        if not (is_dict_badge or is_entity_badge):
            frame.gaps.unread.append(f".views[{view_idx}].badges[{badge_idx}]")
            continue
        if criteria.entity_id is not None and not _badge_matches(
            badge, criteria.entity_id
        ):
            continue
        badge_jq = f".views[{view_idx}].badges[{badge_idx}]"
        split = _CardSplit()
        if criteria.query is not None:
            _split_badge(badge, split)
            frame.gaps.truncation.extend(badge_jq + cut for cut in split.cut)
        hits = _query_hits(split.leaves, criteria.query_lower)
        if hits == []:
            continue
        badge_match: dict[str, Any] = {
            "view_index": view_idx,
            "section_index": None,
            "card_index": None,
            "badge_index": badge_idx,
            "jq_path": badge_jq,
            "card_type": _BADGE_CARD_TYPE,
            "card_config": badge if is_dict_badge else {"entity": badge},
        }
        # A bare-string badge (the common form) is not subscript-assignable, so
        # a python_path spliced into python_transform would raise TypeError.
        # Only advertise python_path for dict badges; string badges must be
        # converted to dict form first.
        if is_dict_badge:
            badge_match["python_path"] = f"['views'][{view_idx}]['badges'][{badge_idx}]"
        if hits is not None:
            badge_match["matched"] = hits
        matches.append(badge_match)
    return matches


def _find_header_card_matches(
    view: dict[str, Any], frame: _CardWalkFrame
) -> list[dict[str, Any]]:
    """Search a sections-view header card (views[n].header.card).

    The header accepts a card (typically Markdown) that can contain entity refs.
    """
    view_idx = frame.view_index
    if not frame.criteria.can_match_cards():
        return []
    header = view.get("header")
    if not isinstance(header, dict):
        frame.gaps.note_unread(header, f".views[{view_idx}].header")
        return []
    if header.get("card") is None:
        return []
    return _walk_card(
        header["card"],
        jq_prefix=f".views[{view_idx}].header.card",
        python_prefix=f"['views'][{view_idx}]['header']['card']",
        frame=frame,
    )


def _find_view_card_matches(
    view: dict[str, Any], frame: _CardWalkFrame
) -> list[dict[str, Any]]:
    """Search a view's top-level cards: its ``cards`` and every section's ``cards``.

    Both are read whatever the view's ``type``: the frontend renders a view
    with ``sections`` and no ``type`` as a sections view, and cards a view type
    does not render still reference entities a rename or removal must find.
    """
    if not frame.criteria.can_match_cards():
        return []
    matches: list[dict[str, Any]] = []
    for base, base_py, cards, section_idx in _view_card_lists(view, frame):
        for card_idx, card in enumerate(cards):
            matches.extend(
                _walk_card(
                    card,
                    jq_prefix=f"{base}[{card_idx}]",
                    python_prefix=f"{base_py}[{card_idx}]",
                    frame=frame.with_indices(
                        section_index=section_idx, card_index=card_idx
                    ),
                )
            )
    return matches


def _view_card_lists(
    view: dict[str, Any], frame: _CardWalkFrame
) -> list[tuple[str, str, list[Any], int | None]]:
    """``(jq path, python path, cards, section index)`` per card list in a view.

    A container present in a shape the walk does not read is recorded in
    ``frame.gaps``.
    """
    view_idx = frame.view_index
    lists: list[tuple[str, str, list[Any], int | None]] = []
    cards = view.get("cards")
    if isinstance(cards, list):
        lists.append(
            (
                f".views[{view_idx}].cards",
                f"['views'][{view_idx}]['cards']",
                cards,
                None,
            )
        )
    else:
        frame.gaps.note_unread(cards, f".views[{view_idx}].cards")
    sections = view.get("sections")
    if not isinstance(sections, list):
        frame.gaps.note_unread(sections, f".views[{view_idx}].sections")
        return lists
    for section_idx, section in enumerate(sections):
        path = f".views[{view_idx}].sections[{section_idx}]"
        if not isinstance(section, dict):
            frame.gaps.unread.append(path)
            continue
        section_cards = section.get("cards")
        if not isinstance(section_cards, list):
            frame.gaps.note_unread(section_cards, f"{path}.cards")
            continue
        lists.append(
            (
                f"{path}.cards",
                f"['views'][{view_idx}]['sections'][{section_idx}]['cards']",
                section_cards,
                section_idx,
            )
        )
    return lists


def _find_cards_matching(
    config: dict[str, Any],
    criteria: _SearchCriteria,
    gaps: _SearchGaps,
) -> list[dict[str, Any]]:
    """Find cards, badges, and header cards in a dashboard config matching ``criteria``.

    ``query`` (case-insensitive) requires a string the card itself holds to
    contain it, and each such match lists the hits under ``matched``. Cards
    are searched in a view's ``cards`` and its sections, in every card slot
    nested at any depth (see ``_split_card_node``), plus view-level badges and
    sections-view header cards.

    Each match carries ``jq_path``, and ``python_path`` (appended after
    ``config`` in ``ha_config_set_dashboard(python_transform)``) except a
    bare-string badge, which is not subscript-assignable. What the walk
    could not search is recorded in ``gaps``: subtrees past the depth bound,
    cards holding one of ``_NON_CARD_CHILD_KEYS``, and entries the walk does
    not read (an untyped card, or a view, section, card list or badge in
    another shape).
    Empty criteria match everything, so callers must reject them first (the
    search entry point, ``_get_dashboard_search_mode``, does).
    """
    views = config.get("views")
    if "strategy" in config:
        return []  # Strategy dashboards hold no stored cards
    if not isinstance(views, list):
        gaps.note_unread(views, ".views")
        return []
    matches: list[dict[str, Any]] = []
    for view_idx, view in enumerate(views):
        if not isinstance(view, dict):
            gaps.unread.append(f".views[{view_idx}]")
            continue
        frame = _CardWalkFrame(
            criteria,
            view_index=view_idx,
            section_index=None,
            card_index=None,
            depth=0,
            gaps=gaps,
        )
        matches.extend(_find_badge_matches_in_view(view, frame))
        matches.extend(_find_header_card_matches(view, frame))
        matches.extend(_find_view_card_matches(view, frame))
    return matches


def _card_matches(card: dict[str, Any], criteria: _SearchCriteria) -> bool:
    """Check if a card matches the entity_id / card_type / heading criteria."""
    if criteria.card_type is not None and card.get("type") != criteria.card_type:
        return False

    if criteria.entity_id is not None:
        rows = card.get("entities")
        values = [card.get("entity")] + [
            row.get("entity") if isinstance(row, dict) else row
            for row in (rows if isinstance(rows, list) else [])
        ]
        # Rows such as dividers carry no entity; _entity_matches skips them.
        if not any(_entity_matches(criteria.entity_id, v) for v in values):
            return False

    if criteria.heading is not None:
        card_heading = card.get("heading", card.get("title"))
        if (
            card_heading is None
            or criteria.heading.lower() not in str(card_heading).lower()
        ):
            return False

    return True


_SEARCH_MATCH_CAP = 200


def _find_cards_in_docs(
    docs: list[dict[str, Any]], criteria: _SearchCriteria, *, name_dashboards: bool
) -> tuple[list[dict[str, Any]], _SearchGaps]:
    """Matches over ``docs`` (``{url_path, config}``) and where the search could not look.

    Each match is stamped with its dashboard's ``url_path`` and ``config_hash``;
    with ``name_dashboards`` each gap location is prefixed with ``<url_path>:``.
    """
    matches: list[dict[str, Any]] = []
    gaps = _SearchGaps()
    for doc in docs:
        # The default dashboard has no url_path; "default" is what the get and
        # set tools accept for it.
        url_path = doc["url_path"] or "default"
        doc_gaps = _SearchGaps()
        found = _find_cards_matching(doc["config"], criteria, doc_gaps)
        if found:
            config_hash = compute_config_hash(doc["config"])
            for match in found:
                match["url_path"] = url_path
                match["config_hash"] = config_hash
        matches.extend(found)
        gaps.extend(doc_gaps, f"{url_path}:" if name_dashboards else "")
    return matches, gaps


def _search_warnings(
    *, truncated: bool, gaps: _SearchGaps, warn_uncovered: bool
) -> list[str]:
    """Warnings that keep an incomplete search from reading as complete.

    Disclosure keys off the *presence* of a match cap, a depth-truncated
    subtree, unsearched picture-elements or an unread entry, not off a
    0-match. ``warn_uncovered`` gates the picture-elements warning.
    """
    uncovered = gaps.uncovered if warn_uncovered else []
    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"Results capped at {_SEARCH_MATCH_CAP} matches; narrow the search "
            "(url_path, card_type, query) for a complete list."
        )
    if gaps.truncation:
        warnings.append(
            f"Search stopped at a nesting depth bound in {len(gaps.truncation)} "
            "place(s); content nested deeper was not searched, so results may "
            "be incomplete."
        )
    if uncovered:
        locations = ", ".join(sorted(set(uncovered)))
        warnings.append(
            "entity_id, card_type and heading do not match inside "
            f"picture-elements 'elements', present at: {locations}. Use query= "
            "to search their text, or fetch the full config to inspect them."
        )
    if gaps.unread:
        warnings.append(
            "Not searched as cards or badges (no 'type' key, or an entry in a "
            f"shape the search does not read): {', '.join(gaps.unread)}. Fetch "
            "the config to see them."
        )
    return warnings


def _unread_dashboards_warning(failed: int | list[str]) -> list[str]:
    """The warning for storage dashboards that could not be read.

    ``failed`` is the component's count (it does not name them) or the url_paths
    the legacy reads collected.
    """
    if not failed:
        return []
    if isinstance(failed, int):
        return [
            f"Could not read {failed} storage dashboard(s), so they were not searched."
        ]
    return [
        f"Could not read {len(failed)} storage dashboard(s), so they were not "
        f"searched: {', '.join(failed)}. Get one by url_path to see its error."
    ]


def build_search_result(
    docs: list[dict[str, Any]],
    *,
    criteria: _SearchCriteria,
    include_config: bool,
    url_path: str | None = None,
    config_suppressed_note: str | None = None,
) -> dict[str, Any]:
    """Run the card search over ``docs`` (``{url_path, config}``) and assemble it.

    ``url_path`` names the one dashboard a scoped search read; its
    ``config_hash`` then also appears at the top level (``None`` across
    dashboards). ``config_suppressed_note`` (a dashboard not provably
    storage-mode) withholds card bodies and the matched values ``query``
    found — a YAML dashboard may carry HA-resolved ``!secret`` plaintext.
    Match LOCATIONS and the matched field names are always reported.
    """
    matches, gaps = _find_cards_in_docs(
        docs, criteria, name_dashboards=url_path is None
    )
    truncated = len(matches) > _SEARCH_MATCH_CAP
    matches = matches[:_SEARCH_MATCH_CAP]
    if not include_config or config_suppressed_note is not None:
        for match in matches:
            del match["card_config"]
    if config_suppressed_note is not None:
        for match in matches:
            if "matched" in match:
                match["matched"] = [{"field": h["field"]} for h in match["matched"]]

    # query reads picture-elements text, and card_type='badge' cannot match
    # a card; only criteria that can select cards make unsearched
    # 'elements' worth a warning.
    warnings = _search_warnings(
        truncated=truncated, gaps=gaps, warn_uncovered=criteria.targets_cards()
    )
    if config_suppressed_note is not None and matches:
        warnings.insert(0, config_suppressed_note)

    if matches:
        hint = (
            "Use a match's python_path and config_hash with "
            "ha_config_set_dashboard(python_transform=...) for targeted updates."
        )
    else:
        hint = (
            "No card matches. Try other criteria, or fetch the full config "
            "(no search params) to inspect the dashboard."
        )
    result: dict[str, Any] = {
        "success": True,
        "action": "search",
        "url_path": url_path,
        "config_hash": (
            compute_config_hash(docs[0]["config"]) if url_path is not None else None
        ),
        "search_criteria": asdict(criteria),
        "matches": matches,
        "match_count": len(matches),
        "truncated": truncated,
        "hint": hint,
    }
    if warnings:
        result["warnings"] = warnings
    return result
