#!/usr/bin/env python3
"""Read-only, exact-match preflight for camera/card-scoped episode clip logs.

Python 3.10+, standard library only. See README.md for the input contract and
limits. The command writes its JSON result to stdout; it never writes media or
logs. A ready manifest is a filename audit, not a media-quality guarantee.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
from dataclasses import dataclass
from typing import Any, Sequence

SCHEMA_VERSION = 1
COLUMNS = ["episode", "camera", "card", "first", "last"]
DEFAULT_EXTENSIONS = frozenset(
    {".mov", ".mp4", ".mxf", ".mts", ".m2ts", ".avi", ".mkv", ".m4v",
     ".webm", ".mpg", ".mpeg", ".r3d", ".braw", ".ari", ".crm"}
)
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_POLICY_BYTES = 2 * 1024 * 1024
MAX_APPROVALS = 10_000
MAX_POLICY_EXPANDED = 100_000
MAX_POLICY_ASSIGNMENTS = 100_000
MAX_DIAGNOSTIC_EPISODES = 5
MAX_SOURCES = 16
MAX_ENTRIES = 100_000
MAX_ROWS = 10_000
MAX_RANGE = 10_000
MAX_EXPANDED = 100_000
MAX_ERRORS = 100
MAX_FIELD_CHARS = 255
IDENTIFIER = re.compile(r"(.*?)([0-9]+)\Z")
EXTENSION = re.compile(r"\.[a-z0-9]+\Z")


class AuditFailure(Exception):
    """An expected failure that must be reported as JSON, never a traceback."""

    def __init__(self, code: str, message: str, **context: Any):
        super().__init__(message)
        self.detail = {"code": code, "message": message, **context}


@dataclass(frozen=True)
class Source:
    index: int
    camera: str
    card: str
    directory: Path

    def output(self) -> dict[str, Any]:
        return {"source_index": self.index, "camera": self.camera,
                "card": self.card, "directory": str(self.directory)}


@dataclass(frozen=True)
class Assignment:
    episode: str
    camera: str
    card: str
    clip_id: str
    record: int


@dataclass(frozen=True)
class ReuseApproval:
    approval: int
    episodes: frozenset[str]


class Blockers:
    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []

    def add(self, code: str, message: str, **context: Any) -> None:
        if len(self.errors) >= MAX_ERRORS:
            raise AuditFailure(
                "diagnostic_limit", "Too many blockers; fix these and rerun.",
                limit=MAX_ERRORS,
            )
        self.errors.append({"code": code, "message": message, **context})


def _valid_text(value: str) -> bool:
    return (bool(value) and value == value.strip()
            and len(value) <= MAX_FIELD_CHARS
            and not any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F
                        or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise AuditFailure("invalid_path", "Paths must be nonempty text without NUL.")
    if any(0xD800 <= ord(c) <= 0xDFFF for c in raw):
        raise AuditFailure("invalid_path", "Paths must be valid Unicode text.")
    # Do not normalize '..' across an unexamined symlink. Check the lexical
    # components first, then canonicalize a path proven not to contain links.
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    for part in [*reversed(path.parents), path]:
        try:
            mode = part.lstat().st_mode
        except OSError as exc:
            raise AuditFailure("path_io", "Cannot inspect path.",
                               path=str(part), reason=str(exc)) from exc
        if stat.S_ISLNK(mode):
            raise AuditFailure("symlink", "Symlink paths are not supported.",
                               path=str(part))
    return Path(os.path.abspath(path))


def _sources(specs: Sequence[Sequence[str]]) -> list[Source]:
    if not specs or len(specs) > MAX_SOURCES:
        raise AuditFailure("source_count", "Provide between 1 and 16 source scopes.",
                           limit=MAX_SOURCES)
    sources: list[Source] = []
    scopes: set[tuple[str, str]] = set()
    identities: set[tuple[int, int]] = set()
    for index, spec in enumerate(specs):
        if len(spec) != 3:
            raise AuditFailure("invalid_source", "Each source needs CAMERA CARD DIRECTORY.")
        camera, card, directory = spec
        if not _valid_text(camera) or not _valid_text(card):
            raise AuditFailure("invalid_source_label", "Source labels must be nonempty "
                               "Unicode text without surrounding whitespace or control characters, at most "
                               "255 characters.", source_index=index)
        scope = (camera, card)
        if scope in scopes:
            raise AuditFailure("duplicate_source", "A camera/card scope may be supplied only once.",
                               camera=camera, card=card)
        path = _absolute_path(directory)
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise AuditFailure("not_directory", "A source must be a directory.", path=str(path))
        identity = (info.st_dev, info.st_ino)
        for previous in sources:
            if (path == previous.directory or path in previous.directory.parents
                    or previous.directory in path.parents):
                raise AuditFailure("overlapping_sources", "Source directory trees must not overlap.",
                                   source_index=index, other_source_index=previous.index)
        if identity in identities:
            raise AuditFailure("overlapping_sources", "Source directories refer to the same directory.",
                               source_index=index)
        scopes.add(scope)
        identities.add(identity)
        sources.append(Source(index, camera, card, path))
    return sources


def _extensions(values: Sequence[str] | None) -> frozenset[str]:
    if values is None:
        return DEFAULT_EXTENSIONS
    if not values:
        raise AuditFailure("invalid_extension", "At least one extension is required.")
    result = set()
    for value in values:
        extension = value.lower()
        if not extension.startswith("."):
            extension = "." + extension
        if not EXTENSION.fullmatch(extension):
            raise AuditFailure("invalid_extension", "Extensions must contain only ASCII letters "
                               "and digits, with an optional leading dot.", extension=value)
        result.add(extension)
    return frozenset(result)


def _validate_csv_quoting(text: str) -> None:
    """Reject stray quotes that csv.reader(strict=True) deliberately tolerates.

    This validates quoting only; the standard-library parser still owns CSV
    decoding, escaped quotes, records, cells and newline handling.
    """
    state = "start"
    line = 1
    previous = ""
    for character in text:
        if state == "quoted":
            if character == '"':
                state = "closed"
        elif state == "closed":
            if character == '"':
                state = "quoted"
            elif character in ",\r\n":
                state = "start"
            else:
                raise AuditFailure("csv_format", "Unexpected character after a closing CSV quote.",
                                   physical_line=line)
        elif character == '"':
            if state != "start":
                raise AuditFailure("csv_format", "A quote inside an unquoted CSV field is invalid.",
                                   physical_line=line)
            state = "quoted"
        elif character in ",\r\n":
            state = "start"
        else:
            state = "unquoted"
        if character == "\r" or (character == "\n" and previous != "\r"):
            line += 1
        previous = character
    if state == "quoted":
        raise AuditFailure("csv_format", "CSV log ends inside a quoted field.", physical_line=line)


def _read_rows(log_path: str | os.PathLike[str]) -> list[tuple[int, list[str]]]:
    path = _absolute_path(log_path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise AuditFailure("not_regular_log", "The CSV log must be a regular file.", path=str(path))
    if info.st_size > MAX_LOG_BYTES:
        raise AuditFailure("log_size_limit", "CSV log exceeds the byte limit.", limit=MAX_LOG_BYTES)
    with path.open("rb") as handle:
        data = handle.read(MAX_LOG_BYTES + 1)
    if len(data) > MAX_LOG_BYTES:
        raise AuditFailure("log_size_limit", "CSV log exceeds the byte limit.", limit=MAX_LOG_BYTES)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise AuditFailure("log_encoding", "CSV log must be UTF-8 (an initial BOM is accepted).") from exc
    if "\x00" in text:
        raise AuditFailure("csv_format", "CSV log must not contain NUL characters.")
    _validate_csv_quoting(text)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    try:
        header = next(reader, None)
        if header != COLUMNS:
            raise AuditFailure("csv_header", "Header must be exactly episode,camera,card,first,last "
                               "in that order, without duplicates or extra columns.", expected=COLUMNS)
        rows: list[tuple[int, list[str]]] = []
        for record, row in enumerate(reader, start=2):
            if len(rows) >= MAX_ROWS:
                raise AuditFailure("row_limit", "CSV log exceeds the record limit.", limit=MAX_ROWS)
            if len(row) != len(COLUMNS):
                raise AuditFailure("csv_columns", "Every CSV record must have exactly five cells.",
                                   record=record, physical_line=reader.line_num, cells=len(row))
            rows.append((record, row))
    except csv.Error as exc:
        raise AuditFailure("csv_format", "Cannot parse CSV log.",
                           physical_line=reader.line_num, reason=str(exc)) from exc
    if not rows:
        raise AuditFailure("empty_log", "CSV log must contain at least one data record.")
    return rows


def _range_parts(first: str, last: str) -> tuple[str, int, int, int]:
    """Apply the same exact, bounded ID-range rules to logs and policies."""
    start_match, end_match = IDENTIFIER.fullmatch(first), IDENTIFIER.fullmatch(last)
    if (not start_match or not end_match or any(c in first + last for c in "/\\")):
        raise AuditFailure("invalid_range", "Each endpoint must end with an ASCII digit run "
                           "and must not contain a path separator.")
    prefix, digits = start_match.groups()
    end_prefix, end_digits = end_match.groups()
    if prefix != end_prefix or len(digits) != len(end_digits):
        raise AuditFailure("incompatible_endpoints", "Endpoints must have identical prefixes "
                           "and digit widths; comparison is case-sensitive.")
    start, end = int(digits), int(end_digits)
    if end < start:
        raise AuditFailure("descending_range", "Range end precedes range start.")
    if end - start + 1 > MAX_RANGE:
        raise AuditFailure("range_limit", "Inclusive range exceeds the per-record clip limit.",
                           limit=MAX_RANGE)
    return prefix, len(digits), start, end


def _policy_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuditFailure("reuse_policy_format", "Duplicate JSON object keys are forbidden.",
                               key=key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise AuditFailure("reuse_policy_format", "Non-finite JSON numbers are forbidden.", value=value)


def _read_reuse_policy(policy_path: str | os.PathLike[str], sources: list[Source]) -> dict[
        tuple[str, str, str], ReuseApproval]:
    path = _absolute_path(policy_path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise AuditFailure("not_regular_policy", "The reuse policy must be a regular file.", path=str(path))
    if info.st_size > MAX_POLICY_BYTES:
        raise AuditFailure("policy_size_limit", "Reuse policy exceeds the byte limit.", limit=MAX_POLICY_BYTES)
    with path.open("rb") as handle:
        data = handle.read(MAX_POLICY_BYTES + 1)
    if len(data) > MAX_POLICY_BYTES:
        raise AuditFailure("policy_size_limit", "Reuse policy exceeds the byte limit.", limit=MAX_POLICY_BYTES)
    try:
        policy = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_policy_object,
                            parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise AuditFailure("reuse_policy_format", "Reuse policy must be valid UTF-8 JSON "
                           "with no excessive nesting.") from exc
    if (not isinstance(policy, dict) or set(policy) != {"schema_version", "approvals"}
            or type(policy["schema_version"]) is not int or policy["schema_version"] != 1
            or not isinstance(policy["approvals"], list) or not policy["approvals"]):
        raise AuditFailure("reuse_policy_schema", "Use exactly schema_version: 1 and a nonempty approvals list.")
    approvals = policy["approvals"]
    if len(approvals) > MAX_APPROVALS:
        raise AuditFailure("policy_approval_limit", "Reuse policy exceeds the approval count limit.",
                           limit=MAX_APPROVALS)
    known = {(source.camera, source.card) for source in sources}
    result: dict[tuple[str, str, str], ReuseApproval] = {}
    expanded = 0
    assignments = 0
    for position, approval in enumerate(approvals, start=1):
        if (not isinstance(approval, dict)
                or set(approval) != {"camera", "card", "first", "last", "episodes"}):
            raise AuditFailure("reuse_policy_schema", "Each approval needs exactly camera, card, "
                               "first, last and episodes; remove unsupported fields.", approval=position)
        if any(not isinstance(approval[field], str) or not _valid_text(approval[field])
               for field in ("camera", "card", "first", "last")):
            raise AuditFailure("reuse_policy_field", "Approval labels and endpoints must follow "
                               "the CSV text rules.", approval=position)
        episodes = approval["episodes"]
        if (not isinstance(episodes, list) or len(episodes) < 2
                or any(not isinstance(episode, str) or not _valid_text(episode) for episode in episodes)
                or len(set(episodes)) != len(episodes)):
            raise AuditFailure("reuse_policy_episodes", "List at least two distinct exact episode "
                               "labels, each following the CSV text rules.", approval=position)
        camera, card = approval["camera"], approval["card"]
        if (camera, card) not in known:
            raise AuditFailure("reuse_policy_unknown_source", "Configure this exact camera/card "
                               "source or remove its approval.", approval=position, camera=camera, card=card)
        try:
            prefix, width, start, end = _range_parts(approval["first"], approval["last"])
        except AuditFailure as exc:
            raise AuditFailure("reuse_policy_range", "Correct the approval range: " + str(exc),
                               approval=position, range_code=exc.detail["code"]) from exc
        expanded += end - start + 1
        assignments += (end - start + 1) * len(episodes)
        if expanded > MAX_POLICY_EXPANDED:
            raise AuditFailure("policy_expanded_limit", "Reuse policy exceeds the expanded-clip limit.",
                               limit=MAX_POLICY_EXPANDED)
        if assignments > MAX_POLICY_ASSIGNMENTS:
            raise AuditFailure("policy_assignment_limit", "Reuse policy exceeds the approved-assignment limit.",
                               limit=MAX_POLICY_ASSIGNMENTS)
        entry = ReuseApproval(position, frozenset(episodes))
        for number in range(start, end + 1):
            clip_id = prefix + str(number).zfill(width)
            key = (camera, card, clip_id)
            if key in result:
                raise AuditFailure("reuse_policy_overlap", "Approval ranges must not overlap; "
                                   "consolidate or remove this approval.", approval=position,
                                   previous_approval=result[key].approval, camera=camera, card=card,
                                   clip_id=clip_id)
            result[key] = entry
    return result


def _validate_reuse(assignments: list[Assignment],
                    reuse: dict[tuple[str, str, str], ReuseApproval], blockers: Blockers) -> None:
    actual: dict[tuple[str, str, str], set[str]] = {}
    for assignment in assignments:
        key = (assignment.camera, assignment.card, assignment.clip_id)
        actual.setdefault(key, set()).add(assignment.episode)
    for (camera, card, clip_id), approval in sorted(reuse.items()):
        episodes = actual.get((camera, card, clip_id), set())
        if episodes != approval.episodes:
            context: dict[str, Any] = {}
            for label, values in (("approved", approval.episodes), ("actual", episodes),
                                  ("missing", approval.episodes - episodes),
                                  ("unapproved", episodes - approval.episodes)):
                context[label + "_episodes"] = sorted(values)[:MAX_DIAGNOSTIC_EPISODES]
                context[label + "_episode_count"] = len(values)
                context[label + "_episodes_truncated"] = len(values) > MAX_DIAGNOSTIC_EPISODES
            blockers.add("reuse_episode_mismatch" if episodes else "unused_reuse_approval",
                         "Each approved clip must appear in exactly the approved episodes. "
                         "Review the log and correct or remove stale approvals; no reuse is accepted.",
                         approval=approval.approval, camera=camera, card=card, clip_id=clip_id,
                         **context)


def _expand(rows: list[tuple[int, list[str]]], sources: list[Source],
            blockers: Blockers,
            reuse: dict[tuple[str, str, str], ReuseApproval] | None = None) -> list[Assignment]:
    known = {(source.camera, source.card) for source in sources}
    assignments: list[Assignment] = []
    seen: dict[tuple[str, str, str], Assignment] = {}
    seen_episodes: dict[tuple[str, str, str, str], Assignment] = {}
    expanded = 0
    for record, row in rows:
        episode, camera, card, first, last = row
        if any(not _valid_text(value) for value in row):
            blockers.add("invalid_field", "Fields must be nonempty, at most 255 characters, "
                         "without surrounding whitespace, control characters or invalid Unicode.",
                         record=record)
            continue
        if (camera, card) not in known:
            blockers.add("unknown_source", "No source is configured for this exact camera/card scope.",
                         record=record, camera=camera, card=card)
            continue
        try:
            prefix, width, start, end = _range_parts(first, last)
        except AuditFailure as exc:
            blockers.add(**exc.detail, record=record)
            continue
        count = end - start + 1
        expanded += count
        if expanded > MAX_EXPANDED:
            raise AuditFailure("expanded_limit", "CSV log exceeds the total expanded-clip limit.",
                               limit=MAX_EXPANDED)
        for number in range(start, end + 1):
            clip_id = prefix + str(number).zfill(width)
            assignment = Assignment(episode, camera, card, clip_id, record)
            key = (camera, card, clip_id)
            episode_key = (*key, episode)
            previous = seen_episodes.get(episode_key) or seen.get(key)
            if previous is not None:
                if previous.episode == episode or reuse is None or key not in reuse:
                    code = "duplicate_assignment" if previous.episode == episode else "cross_episode_reuse"
                    message = "A scoped clip may be assigned only once per audit."
                    if reuse is not None:
                        message += " Review the log and approve only exact intentional cross-episode reuse."
                    blockers.add(code, message,
                                 record=record, previous_record=previous.record, episode=episode,
                                 previous_episode=previous.episode, camera=camera, card=card, clip_id=clip_id)
                    continue
            seen.setdefault(key, assignment)
            seen_episodes[episode_key] = assignment
            assignments.append(assignment)
    return assignments


def _index(sources: list[Source], extensions: frozenset[str]) -> tuple[
        dict[tuple[str, str, str], list[dict[str, Any]]], dict[str, int],
        dict[tuple[int, str], tuple[int, int]]]:
    index: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    identities: dict[tuple[int, str], tuple[int, int]] = {}
    counts = {"scanned_entries": 0, "media_files": 0, "ignored_files": 0}
    for source in sources:
        pending = [source.directory]
        while pending:
            directory = pending.pop()
            # Iteration itself can fail; never treat an interrupted scan as complete.
            with os.scandir(directory) as iterator:
                entries = []
                for entry in iterator:
                    counts["scanned_entries"] += 1
                    if counts["scanned_entries"] > MAX_ENTRIES:
                        raise AuditFailure("entry_limit", "Source trees exceed the combined entry limit.",
                                           limit=MAX_ENTRIES)
                    if any(0xD800 <= ord(c) <= 0xDFFF for c in entry.name):
                        raise AuditFailure("filename_encoding", "A filename is not valid Unicode text.",
                                           source_index=source.index)
                    entries.append(entry)
            for entry in sorted(entries, key=lambda item: item.name):
                relative = Path(entry.path).relative_to(source.directory).as_posix()
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise AuditFailure("symlink", "Symlinks block the audit, including non-media links.",
                                       source_index=source.index, relative_path=relative)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                    continue
                path = Path(entry.name)
                if not stat.S_ISREG(info.st_mode):
                    raise AuditFailure("special_file", "Non-regular, non-directory entries block the audit.",
                                       source_index=source.index, relative_path=relative)
                if path.suffix.lower() not in extensions:
                    counts["ignored_files"] += 1
                    continue
                key = (source.camera, source.card, path.stem)
                index.setdefault(key, []).append({"source_index": source.index, "relative_path": relative})
                identities[(source.index, relative)] = (info.st_dev, info.st_ino)
                counts["media_files"] += 1
    for candidates in index.values():
        candidates.sort(key=lambda item: (item["source_index"], item["relative_path"]))
    return index, counts, identities


def audit(log_path: str | os.PathLike[str], source_specs: Sequence[Sequence[str]],
          extensions: Sequence[str] | None = None,
          reuse_policy: str | os.PathLike[str] | None = None) -> tuple[dict[str, Any], int]:
    """Return (JSON-compatible report, exit code). Never emit a partial manifest."""
    report: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "status": "error", "errors": []}
    blockers = Blockers()
    try:
        sources = _sources(source_specs)
        allowed = _extensions(extensions)
        report["sources"] = [source.output() for source in sources]
        report["extensions"] = sorted(allowed)
        reuse = _read_reuse_policy(reuse_policy, sources) if reuse_policy is not None else None
        rows = _read_rows(log_path)
        assignments = _expand(rows, sources, blockers, reuse)
        report["counts"] = {"log_records": len(rows), "unique_assignments": len(assignments)}
        if reuse is not None:
            report["counts"]["unique_scoped_clips"] = len({
                (assignment.camera, assignment.card, assignment.clip_id) for assignment in assignments})
            _validate_reuse(assignments, reuse, blockers)
        if blockers.errors:
            report.update(status="blocked", errors=blockers.errors)
            return report, 1
        index, counts, identities = _index(sources, allowed)
        report["counts"].update(counts)
        by_episode: dict[str, list[dict[str, Any]]] = {}
        requested_files: dict[tuple[int, int], tuple[Assignment, dict[str, Any]]] = {}
        for assignment in assignments:
            candidates = index.get((assignment.camera, assignment.card, assignment.clip_id), [])
            context = {"record": assignment.record, "episode": assignment.episode,
                       "camera": assignment.camera, "card": assignment.card, "clip_id": assignment.clip_id}
            if not candidates:
                blockers.add("missing_clip", "No regular media file has this exact stem in its source scope.",
                             **context)
            elif len(candidates) != 1:
                blockers.add("ambiguous_clip", "Multiple media files have this exact stem in its source scope.",
                             **context, candidates=candidates)
            else:
                candidate = candidates[0]
                identity = identities[(candidate["source_index"], candidate["relative_path"])]
                if not identity[1]:
                    raise AuditFailure("file_identity_unavailable", "The filesystem did not provide "
                                       "a usable identity for a requested file.", **context, **candidate)
                previous = requested_files.get(identity)
                if previous is not None:
                    previous_assignment, previous_candidate = previous
                    key = (assignment.camera, assignment.card, assignment.clip_id)
                    previous_key = (previous_assignment.camera, previous_assignment.card,
                                    previous_assignment.clip_id)
                    approved_repeat = (reuse is not None and key in reuse and key == previous_key
                                       and candidate == previous_candidate
                                       and assignment.episode != previous_assignment.episode)
                    if not approved_repeat:
                        blockers.add("physical_clip_reuse", "Two requested paths identify the same "
                                     "filesystem file (for example, hardlink aliases).", **context, **candidate,
                                     previous_record=previous_assignment.record,
                                     previous_episode=previous_assignment.episode,
                                     previous_source_index=previous_candidate["source_index"],
                                     previous_relative_path=previous_candidate["relative_path"])
                        continue
                else:
                    requested_files[identity] = (assignment, candidate)
                by_episode.setdefault(assignment.episode, []).append({
                    "camera": assignment.camera, "card": assignment.card, "clip_id": assignment.clip_id,
                    "record": assignment.record, **candidate,
                })
        if blockers.errors:
            report.update(status="blocked", errors=blockers.errors)
            return report, 1
        if reuse is not None:
            report["accepted_reuse"] = [
                {"camera": camera, "card": card, "clip_id": clip_id,
                 "episodes": sorted(approval.episodes)}
                for (camera, card, clip_id), approval in sorted(reuse.items())
            ]
            report["counts"]["requested_physical_files"] = len(requested_files)
        report["manifests"] = [
            {"episode": episode, "clips": sorted(clips, key=lambda clip: (
                clip["camera"], clip["card"], clip["clip_id"], clip["relative_path"]))}
            for episode, clips in sorted(by_episode.items())
        ]
        report["status"] = "ready"
        return report, 0
    except AuditFailure as exc:
        report["errors"] = [*blockers.errors, exc.detail]
    except (OSError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        # OS failures include denied/missing paths and interrupted enumeration.
        # No guessing or partial output when the filesystem cannot be audited.
        report["errors"] = [*blockers.errors, {"code": "io_error", "message": "Audit could not complete.",
                                                 "reason": str(exc)}]
    return report, 2


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AuditFailure("invalid_arguments", message)


def main(argv: Sequence[str] | None = None) -> int:
    parser = JsonArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("log", help="UTF-8 CSV with header episode,camera,card,first,last")
    parser.add_argument("--source", nargs=3, action="append", required=True,
                        metavar=("CAMERA", "CARD", "DIRECTORY"),
                        help="one local directory tree for a unique, case-sensitive scope; repeat as needed")
    parser.add_argument("--extension", action="append",
                        help="allowed media extension; repeat to REPLACE the default allowlist")
    parser.add_argument("--reuse-policy", metavar="PATH",
                        help="optional UTF-8 JSON approvals for exact scoped IDs and episode sets")
    try:
        arguments = parser.parse_args(argv)
        report, code = audit(arguments.log, arguments.source, arguments.extension, arguments.reuse_policy)
    except AuditFailure as exc:
        report = {"schema_version": SCHEMA_VERSION, "status": "error", "errors": [exc.detail]}
        code = 2
    # ASCII escaping handles every valid Unicode label even under a narrow
    # console encoding. Stable keys/newline and no timestamps make runs diffable.
    try:
        sys.stdout.write(json.dumps(report, ensure_ascii=True, sort_keys=True, indent=2) + "\n")
    except (OSError, UnicodeError):
        # If stdout itself is closed/unwritable, no JSON can be delivered there.
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
