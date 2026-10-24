"""Bounded log window values and pagination shared by native and remote readers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorLogPage:
    """One bounded window of the error log, plus whether older history exists.

    ``has_more`` cannot be derived by the caller from ``text``: a journald
    window that comes back full may or may not have anything behind it (see
    ``_journald_error_log_page``), and only the fetching route knows which.
    """

    text: str
    has_more: bool


# Smallest log window this client requests. Supervisor's Range syntax addresses
# a newest-anchored window by its first entry, so a one-entry one is
# inexpressible (``entries=:0:1`` reads from the OLDEST entry instead);
# Supervisor coerces its own ``?lines=1`` to 2 (supervisor/api/host.py) and this
# client mirrors that on every route, so all install types serve the same slice.
# Shared with the tool layer, which sizes its fetch window against it.
MIN_LOG_WINDOW_LINES = 2
# Entries the has_more probe reads from just behind a full window. A one-entry
# probe compared one rendered line, which a boundary duplicate (the same
# timestamp-less traceback line logged twice) could false-match into "end of
# history". Comparing a block this deep requires that many CONSECUTIVE
# identical lines to straddle the boundary — and then the history a false stop
# skips is more of the same duplicates. Timestamps cannot close the gap
# instead: the colliding lines are exactly the ones without them, and
# journald's cursor/timestamp metadata exists only in the journal export
# format, which the hassio-proxy route cannot request.
_PROBE_ENTRIES = 8


def _journald_range_header(lines: int, offset: int = 0) -> str:
    """Build Supervisor's journald ``Range`` header for a bounded window.

    Supervisor's log endpoints accept
    ``Range: entries=<cursor>:<num_skip>:<num_entries>`` and build
    ``entries=:-{N-1}:{N}`` themselves for a ``?lines=N`` request
    (supervisor/api/host.py). ``offset`` moves the window that many entries
    further back, so the returned slice ends ``offset`` entries before the
    newest one.

    A ``lines=1`` window is only expressible with a non-zero ``offset``: at
    ``offset=0`` the header degenerates to ``entries=:0:1``, whose
    non-negative skip reads from the OLDEST entry instead of the newest.
    Callers anchoring at the newest entry pass ``lines`` already floored at
    ``MIN_LOG_WINDOW_LINES``; the one-entry probe always carries an offset.
    """
    # Negate the computed skip rather than prefixing "-" so the degenerate
    # skip of 0 renders as ":0:" (non-negative, reads from the oldest
    # entry) instead of the ill-formed ":-0:".
    return f"entries=:{-(offset + lines - 1)}:{lines}"


def _container_error_log_page(text: str, lines: int, offset: int) -> ErrorLogPage:
    """Window the whole-file ``/api/error_log`` body client-side.

    Same tail semantics as the journald Range the Supervisor-backed
    branches request, so ``get_error_log`` answers with the same slice of
    history on every install type. Paging is exact here — the whole file is
    in hand, so ``has_more`` is simply whether anything precedes the window.
    An ``offset`` past the start of the file yields an empty final page.
    """
    all_lines = text.splitlines()
    end = len(all_lines) - offset
    if end <= 0:
        return ErrorLogPage(text="", has_more=False)
    start = max(end - lines, 0)
    return ErrorLogPage(text="\n".join(all_lines[start:end]), has_more=start > 0)


async def _journald_error_log_page(
    lines: int,
    offset: int,
    fetch: Callable[[int, int], Awaitable[str]],
) -> ErrorLogPage:
    """Read one journald window and settle ``has_more`` with a 1-entry probe.

    systemd's journal-gatewayd does not guard the negative-skip branch the
    way it guards the positive one (``sd_journal_previous_skip(-n_skip + 1)``
    returns no END_OF_STREAM): an offset past the start of the journal
    CLAMPS to the oldest entry and still answers with a full window. So a
    saturated window proves nothing about what lies behind it, and a
    count-based ``has_more`` would stay true forever — an agent following
    the pagination hint would loop on identical pages.

    Hence: a short or empty window means the journal really is exhausted
    (nothing clamps a window smaller), and a saturated one is settled by
    asking for the ``_PROBE_ENTRIES`` entries that should sit just behind
    it. If the journal has nothing there, the clamp starts the probe at
    the same oldest entry the window starts with, so the probe block
    equals the window's opening lines — identity means end of history. A
    differing block proves deeper history (an under-full span behind the
    window also clamps and misaligns, which correctly reads as "more").
    The identity check also terminates the degraded case where an
    intermediary strips the ``Range`` header (RFC 7233 permits it): both
    requests then return Supervisor's default window and compare equal.

    The saturation test counts lines only to decide whether to probe, never
    to answer: one journald entry can be a multi-line traceback, so the line
    count over-estimates entries and can only over-trigger the probe, which
    then gives the real answer. In practice the two rarely diverge here:
    docker's journald log driver — how HAOS captures Core's output —
    ingests stdout line by line, one journal entry per rendered line, so
    multi-line entries reach this endpoint only from native journal-API
    writers.

    Known imprecision, accepted: the identity check can still stop one
    page early when the compared block — ``_PROBE_ENTRIES`` lines, or the
    window's own length when that is smaller — is byte-identical across
    the boundary (see the constant's comment for why timestamps cannot
    disambiguate instead). The failure
    direction is a missed page of exactly those duplicates — bounded,
    unlike the infinite loop a count-based ``has_more`` produces on the
    clamp.
    """
    text = await fetch(lines, offset)
    if not text or len(text.splitlines()) < lines:
        return ErrorLogPage(text=text, has_more=False)
    probe = await fetch(_PROBE_ENTRIES, offset + lines)
    probe_lines = probe.splitlines()
    window_lines = text.splitlines()
    # Compare the shared-length prefix: with a window smaller than the
    # probe (lines < _PROBE_ENTRIES), an end-of-history clamp returns a
    # probe LONGER than the window, and a full-length compare would read
    # that as "more history" forever — the small-limit loop this method
    # exists to prevent.
    shared = min(len(probe_lines), len(window_lines))
    return ErrorLogPage(
        text=text,
        has_more=shared > 0 and probe_lines[:shared] != window_lines[:shared],
    )
