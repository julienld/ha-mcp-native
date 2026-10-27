"""YAML fragment lookup shared by local file owners and legacy service adapters."""

import asyncio
import re
from typing import Any

from ha_mcp.errors import ErrorCode, create_error_response

from .errors import raise_failure

# fnmatch metacharacters. Matching itself happens component-side (list_files
# fnmatches on the file name); this only decides single-file vs. glob.
_GLOB_METACHARS = ("*", "?", "[")

# The metacharacters that match their own literal names, and so are pattern
# syntax rather than a plausible spelling of a file name the caller means
# exactly. A bracket class is the ambiguous one and is deliberately absent.
_PATTERN_ONLY_METACHARS = ("*", "?")

# Wraps each metacharacter in a bracket class so fnmatch reads it literally.
# Deliberately not glob.escape: it splits a drive off first and returns that
# part UNESCAPED, and the split is ntpath on a Windows-hosted server - where a
# name like \\weird[a].yaml is taken for a drive in its entirety and comes back
# with its bracket intact. The one host-dependent step in a path that is
# otherwise POSIX and config-relative.
_METACHAR_RE = re.compile(r"([*?\[])")


def _has_glob(name: str) -> bool:
    """True if ``name`` carries an fnmatch wildcard and must be expanded.

    Takes the file name rather than the whole path: the component resolves
    directories literally, so a metacharacter in a folder name belongs to that
    folder's name and is not a pattern.
    """
    return any(char in name for char in _GLOB_METACHARS)


def _names_a_file(pattern: str) -> bool:
    """True if the request can be read as naming one file rather than a class.

    ``*`` and ``?`` are pattern syntax: they match their own literal names, so
    a caller who wrote one wrote a pattern. A bracket class alone is ambiguous,
    since such a name is legal on the filesystem and cannot match itself.

    One predicate for two decisions that must not diverge - whether a path the
    literal lookup returned counts as explicitly named, and whether the absence
    of such a path is worth a warning. Honouring it in the warning alone would
    leave a file genuinely called ``[ab]*.yaml`` strict, so one failed read of
    it would discard every match the pattern half had already found: the shape
    removed for ``*.yaml`` when provenance moved into the resolver, reachable
    again through the other lookup.
    """
    return not any(char in pattern for char in _PATTERN_ONLY_METACHARS)


def _call_failed(service: str, context: dict[str, Any]) -> None:
    """Raise the shared 'unexpected response' error for a component service."""
    raise_failure(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Unexpected response format from {service} service",
            context=context,
        )
    )


def _unwrap_or_raise(result: Any, service: str, context: dict[str, Any]) -> Any:
    """Unwrap a component service response, raising on a failure payload."""
    if not isinstance(result, dict):
        _call_failed(service, context)
    unwrapped = result
    if not unwrapped.get("success", True):
        raise_failure(unwrapped)
    return unwrapped


def _failure_text(payload: dict[str, Any]) -> str:
    """Readable reason out of a failure payload, for a per-file warning.

    The component answers with ``error`` as a plain string; the shared
    ``create_error_response`` shape nests a ``message`` under it instead.
    """
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error)
    return str(error or "read failed")


def _parse_error_result(
    target: str, yaml_path: str, parse_error: str, *, is_expanded: bool
) -> tuple[None, str]:
    """Decide a file the component could not parse: raise for a named target.

    The explicitly named target raises rather than soft-degrading to a warning
    that reads as "key absent" - it raises even when expansion turned up
    siblings alongside it, discarding their results, because a named file that
    cannot be parsed is the caller's question, not a stray unreadable neighbour.
    An expanded target stays a per-file warning so one broken file does not
    sink the whole search.
    """
    if not is_expanded:
        raise_failure(
            create_error_response(
                ErrorCode.CONFIG_INVALID,
                f"{target} could not be parsed as YAML: {parse_error}",
                suggestions=[f"Fix the YAML syntax error in {target} and retry."],
                context={"file": target, "yaml_path": yaml_path},
            )
        )
    return None, f"{target} was not searched: {parse_error}."


def _read_exception_result(
    target: str, error: BaseException, *, is_expanded: bool
) -> tuple[None, str]:
    """Decide a read that blew up outright.

    An expanded target degrades to a warning so one unreadable file cannot sink
    the search; the explicitly named target raises, as it does for every other
    failure class in :func:`_evaluate_read`. The error is re-raised as it came
    rather than wrapped, so the tool's own handler classifies it by type - the
    other classes have no live exception to preserve and build a structured
    :class:`ToolError` instead.

    Anything that is not an ``Exception`` propagates whatever the target's
    provenance - this is the one failure class here that raises for an expanded
    target too. Cancellation and shutdown are not one file being unreadable,
    and absorbing them into a warning next to ``success: true`` would report a
    search that was called off as one that ran and found nothing. The per-item
    fan-outs in ``tools_entities`` and ``tools_search`` draw the same line.

    ``str()`` is empty on a bare ``TimeoutError()``, so the class name stands
    in - a warning reading "was not searched: ." names no reason at all.
    """
    if not is_expanded or not isinstance(error, Exception):
        raise error
    return None, f"{target} was not searched: {str(error) or type(error).__name__}."


def _evaluate_read(
    response: Any,
    target: str,
    yaml_path: str,
    *,
    is_expanded: bool,
    include_content: bool,
    include_parsed: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    """Turn one ``read_file`` response into a match, a warning, or neither.

    Three outcomes, in the order they are decided:

    * **warning** — the file could not be searched at all. An expanded target
      must not sink the whole search and discard the matches already found:
      expansion is not restricted to ``*.yaml``, so ``packages/*`` can turn up
      a README the component refuses to read, and a permission-denied or
      non-UTF-8 ``.yaml`` lands here too. A syntactically broken file is the
      same class — reporting it as "key absent" would claim a file was
      inspected when it never was. The explicitly named target raises
      instead, even when expansion turned up siblings alongside it.
    * **neither** — the file parsed and simply has no such key. The real
      non-match, and the whole point of a glob search.
    * **match** — the key is there.
    """
    if isinstance(response, BaseException):
        return _read_exception_result(target, response, is_expanded=is_expanded)

    if not isinstance(response, dict):
        if is_expanded:
            return None, f"{target} was not searched: unexpected read_file response."
        _call_failed("read_file", {"file": target, "yaml_path": yaml_path})
    unwrapped = response

    if not unwrapped.get("success", True):
        if not is_expanded:
            raise_failure(unwrapped)
        return None, f"{target} was not searched: {_failure_text(unwrapped)}."

    parse_error = unwrapped.get("parse_error")
    if parse_error:
        return _parse_error_result(
            target, yaml_path, str(parse_error), is_expanded=is_expanded
        )

    if unwrapped.get("subtree") is None:
        return None, None

    match: dict[str, Any] = {"file": target, "yaml_path": yaml_path}
    if include_content:
        match["content"] = unwrapped["subtree"]
    if include_parsed:
        match["parsed"] = unwrapped.get("parsed")
    return match, None


async def _list_files_matching(
    files: Any, file: str, directory: str, pattern: str
) -> list[str]:
    """Return the non-directory paths in ``directory`` matching ``pattern``."""
    unwrapped = _unwrap_or_raise(
        await files.list_files(path=directory or ".", pattern=pattern),
        "list_files",
        {"file": file},
    )
    entries = unwrapped.get("files")
    if not isinstance(entries, list):
        _call_failed("list_files", {"file": file})
    # Sorted so a multi-file result is deterministic across calls.
    return sorted(
        str(entry["path"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("path") and not entry.get("is_dir")
    )


def _absent_literal_warnings(
    file: str, pattern: str, *, literal_found: bool, target_count: int
) -> list[str]:
    """Say so when nothing is literally named the way a bracketed request was.

    Silence is what the caller reads as "your file was searched": the siblings
    the class matched arrive as ``files_searched`` with nothing marking the
    named file as never opened.

    Two cases stay silent. A literal that was found needs no warning, and
    neither does a request that :func:`_names_a_file` rejects - nobody named a
    file, so nothing is missing. The literal lookup still runs for those, since
    its results are merged as ordinary expansion matches; only the complaint is
    dropped.
    """
    if literal_found or not _names_a_file(pattern):
        return []
    if not target_count:
        # Phrased for the degenerate case rather than reporting "the 0 file(s)
        # matching it as a pattern", which reads as a search that happened.
        return [
            f"No file is literally named {file}, and nothing matched it as a "
            f"pattern either."
        ]
    return [
        f"No file is literally named {file}; searched the {target_count} "
        f"file(s) matching it as a pattern."
    ]


async def _resolve_target_files(
    files: Any, file: str
) -> tuple[list[tuple[str, bool]], list[str]]:
    """Expand ``file`` into ``(path, is_expanded)`` pairs plus any warnings.

    ``is_expanded`` records where the path came from, and it is decided here
    because only here is it known: a path produced by the pattern lookup is an
    expansion, while the requested path itself, and what the escaped-literal
    lookup returns for a request :func:`_names_a_file` accepts, were named
    explicitly. Recovering that downstream by comparing the path against the
    requested string would be wrong for ``*`` and ``?``, which match their own
    literal names - a file genuinely called ``*.yaml`` would then be mistaken
    for the name the caller typed and its read failure would sink the whole
    search.

    A plain path is returned as-is (the read itself reports a missing file). A
    glob is expanded through the component's ``list_files``, which matches the
    file name only, so ``packages/*.yaml`` covers one directory level rather
    than a nested tree. Only the name is inspected for metacharacters: the
    component resolves the directory literally, so ``pack[a]ges/svc.yaml``
    names one file and is read as one rather than expanded.

    A bracketed name is ambiguous: ``packages/svc[a].yaml`` is both a valid
    pattern and a valid filename, and a closed bracket class is the one
    metacharacter form whose pattern cannot match its own name (``fnmatch``
    reads ``[a]`` as the single character ``a``, while ``*`` and ``?`` do match
    themselves). Such a name is therefore looked up twice, as the pattern and
    as the escaped literal, and the results are merged: the pattern alone would
    report ``files_searched: 0`` for a file that exists, and the literal alone
    would drop the siblings a deliberate class like ``svc[12].yaml`` is meant
    to find. Both lookups run because a sibling that satisfies the class would
    otherwise mask the file the caller named exactly. Only bracketed names pay
    the second lookup, and it is issued concurrently with the first rather than
    after it. The component likewise treats package folder names carrying
    metacharacters as literal names (#1854).

    The guard is any bracket, not a closed class: an unclosed or empty bracket
    is literal to ``fnmatch`` and so matches its own name, which means both
    lookups return it and the deduplication below is what keeps it a single
    target. A listing failure on either lookup raises rather than degrading -
    the directory is the same one in both calls, so a failure there is a broken
    request, not one unreadable file among several.

    When the literal lookup comes back empty the caller named a file that does
    not exist, and the pattern's siblings would otherwise be reported as if the
    named file had been searched; :func:`_absent_literal_warnings` decides what
    to say about that.
    """
    directory, _, pattern = file.rpartition("/")
    if not _has_glob(pattern):
        return [(file, False)], []

    if "[" not in pattern:
        matched = await _list_files_matching(files, file, directory, pattern)
        return [(target, True) for target in matched], []

    # The two lookups are independent, so run them together rather than paying
    # two sequential round-trips; the reads below fan out for the same reason.
    matched, literal = await asyncio.gather(
        _list_files_matching(files, file, directory, pattern),
        _list_files_matching(
            files, file, directory, _METACHAR_RE.sub(r"[\1]", pattern)
        ),
    )
    # Both lookups always contribute targets; only whether a literal hit counts
    # as *named* depends on the request. Under a pattern it is merged as an
    # ordinary expansion match, so nobody named it and a bad read stays a
    # warning.
    literal_paths = set(literal)
    named = literal_paths if _names_a_file(pattern) else set()
    targets = sorted(set(matched) | literal_paths)
    warnings = _absent_literal_warnings(
        file, pattern, literal_found=bool(named), target_count=len(targets)
    )
    return [(target, target not in named) for target in targets], warnings


class YamlLookup:
    def __init__(self, files: Any) -> None:
        self.files = files

    async def get(
        self,
        yaml_path: str,
        file: str = "configuration.yaml",
        include_content: bool = True,
        include_parsed: bool = False,
    ) -> dict[str, Any]:
        targets, resolve_warnings = await _resolve_target_files(self.files, file)

        extra: dict[str, Any] = {"include_parsed": True} if include_parsed else {}
        # The reads are independent, so fan them out rather than paying N
        # sequential round-trips to HA — a packages glob is routinely 10+
        # files. gather preserves order, so matches stay sorted by file.
        # An expanded target blowing up must not sink the search, so every
        # exception comes back as a value; whether it degrades to a warning
        # or is re-raised is then decided per target, like every other
        # failure class.
        responses = await asyncio.gather(
            *(
                self.files.read_file(path=target, yaml_path=yaml_path, **extra)
                for target, _ in targets
            ),
            return_exceptions=True,
        )

        matches: list[dict[str, Any]] = []
        warnings: list[str] = list(resolve_warnings)
        files_unreadable = 0
        for (target, is_expanded), response in zip(targets, responses, strict=True):
            match, warning = _evaluate_read(
                response,
                target,
                yaml_path,
                is_expanded=is_expanded,
                include_content=include_content,
                include_parsed=include_parsed,
            )
            if warning:
                warnings.append(warning)
                files_unreadable += 1
            elif match:
                matches.append(match)

        result: dict[str, Any] = {
            "success": True,
            "yaml_path": yaml_path,
            "matches": matches,
            "count": len(matches),
            # Separates "the glob matched no files" from "no file defines
            # the key" — same empty matches[], different fix.
            "files_searched": len(targets),
            # Keeps `count: 0` beside a non-zero files_searched from reading
            # as "searched them, found nothing". Counted in the loop, not as
            # len(warnings): those also carry resolver-level entries.
            "files_unreadable": files_unreadable,
        }
        if warnings:
            result["warnings"] = warnings
        return result
