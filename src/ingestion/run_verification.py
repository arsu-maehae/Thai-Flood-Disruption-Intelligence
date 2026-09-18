"""Read-only schema-1.2 verification, supported only with no active writer.

This is not a lock or concurrency protection. Published records alone establish
counts; interrupted requests and uncertain source-publication remnants cannot be
reconstructed. Original-response hashes/counts are declarations, not independently
verified evidence. No configuration loading, networking, repairs, or writes occur.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qsl, unquote, unquote_plus, urlsplit

_RUNS = Path("gistda/flood_freq/pattani/runs")
_POLICY = "observed_offset_limit_v1"
_DISCLAIMER = (
    "Ordering and termination are observed implementation policy, not an "
    "official GISTDA contract."
)
_CREDENTIAL_NAMES = frozenset({"api_key", "api-key", "authorization"})
_COUNT_NAMES = (
    "attempted_request_count", "persisted_page_count", "validated_page_count",
    "journaled_page_count",
)
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in range(1, 10)
}


@dataclass(frozen=True)
class VerificationCounts:
    """Last verified published snapshot, not an estimate of in-flight work."""

    attempted_request_count: int | None
    persisted_page_count: int | None
    validated_page_count: int | None
    journaled_page_count: int

    def to_dict(self) -> dict[str, int | None]:
        return {name: getattr(self, name) for name in _COUNT_NAMES}


@dataclass(frozen=True)
class RunVerificationReport:
    """Only validated identifiers, fixed categories, booleans and counts escape."""

    run_id: str | None
    status: str
    terminal_kind: str | None = None
    journal_page_count: int = 0
    last_published_counts: VerificationCounts | None = None
    counts_final: bool = False
    issue_categories: tuple[str, ...] = ()
    temporary_remnant_count: int = 0
    unexpected_entry_count: int = 0
    configured_key_check_complete: bool = False
    observed_total_features: int | None = None
    observed_number_matched_present: bool | None = None
    observed_number_matched: int | None = None
    original_response_independently_verified: bool = False

    def to_dict(self) -> dict[str, object]:
        result = {name: getattr(self, name) for name in self.__dataclass_fields__}
        result["issue_categories"] = list(self.issue_categories)
        result["last_published_counts"] = (
            self.last_published_counts.to_dict() if self.last_published_counts else None
        )
        return result


class _Invalid(Exception):
    """Internal fixed-category control flow; never carries inspected text."""


def _require(condition: bool, category: str) -> None:
    if not condition:
        raise _Invalid(category)


def _integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _link(info) -> bool:
    # Windows junctions may not have S_IFLNK, but must not redirect traversal.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) &
        getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _representations(value: str):
    pending = [value]
    seen: set[str] = set()
    while pending:
        text = pending.pop()
        if text in seen:
            continue
        seen.add(text)
        yield text
        for decode in (unquote, unquote_plus):
            decoded = decode(text)
            if decoded not in seen:
                pending.append(decoded)


def _credential_name(value: str) -> bool:
    return any(text.casefold() in _CREDENTIAL_NAMES for text in _representations(value))


def _text(value: str, api_key: str | None) -> None:
    for text in _representations(value):
        _require(not api_key or api_key not in text, "credential_rejected")
        _require(not re.search(r"(?:API-Key|Authorization)\s*:", text, re.I),
                 "credential_rejected")
        for name, _ in parse_qsl(urlsplit(text).query, keep_blank_values=True):
            _require(not _credential_name(name), "credential_rejected")


def _tree(value: object, api_key: str | None) -> None:
    if isinstance(value, dict):
        for name, child in value.items():
            _require(not _credential_name(name), "credential_rejected")
            _text(name, api_key)
            _tree(child, api_key)
    elif isinstance(value, list):
        for child in value:
            _tree(child, api_key)
    elif isinstance(value, str):
        _text(value, api_key)


def _pairs(pairs):
    result = {}
    for name, value in pairs:
        _require(name not in result, "duplicate_json_keys")
        result[name] = value
    return result


def _finite(text: str) -> float:
    value = float(text)
    _require(math.isfinite(value), "nonfinite_json_number")
    return value


def _constant(_text: str):
    raise _Invalid("nonfinite_json_number")


def _timestamp(value: object) -> datetime:
    _require(isinstance(value, str), "invalid_timestamp")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(timestamp.utcoffset() == timedelta(0), "invalid_timestamp")
        return timestamp
    except (ValueError, TypeError):
        raise _Invalid("invalid_timestamp") from None


class _Reader:
    def __init__(self, root: Path, api_key: str | None):
        self.root = root
        self.api_key = api_key

    def contained(self, relative: object, *, directory: bool = False) -> Path:
        _require(isinstance(relative, str) and bool(relative), "invalid_path")
        _text(relative, self.api_key)
        windows = PureWindowsPath(relative)
        path = Path(relative)
        _require(not path.is_absolute() and not windows.drive and not windows.root
                 and ".." not in windows.parts and ".." not in path.parts
                 and bool(path.parts), "external_path")
        target = self.root
        for index, component in enumerate(path.parts):
            target = target / component
            info = target.lstat()
            mode = info.st_mode
            _require(not _link(info), "symlink_rejected")
            if index < len(path.parts) - 1 or directory:
                _require(stat.S_ISDIR(mode), "nonregular_entry")
            else:
                _require(stat.S_ISREG(mode), "nonregular_entry")
        _require(target.resolve().is_relative_to(self.root), "external_path")
        return target

    def document(self, relative: str) -> tuple[dict, bytes]:
        path = self.contained(relative)
        content = path.read_bytes()
        if self.api_key:
            _require(self.api_key.encode("utf-8") not in content, "credential_rejected")
        try:
            value = json.loads(content, object_pairs_hook=_pairs, parse_float=_finite,
                               parse_constant=_constant)
        except (ValueError, UnicodeError):
            raise _Invalid("invalid_json") from None
        _require(isinstance(value, dict), "nonobject_document")
        _tree(value, self.api_key)
        return value, content


def _version(record: dict, run_id: str) -> None:
    _require(record.get("journal_schema_version") == "1.2", "unsupported_schema")
    _require(record.get("pipeline_policy_version") == "1.0", "unsupported_policy_version")
    if "run_id" in record:
        _require(record["run_id"] == run_id, "run_id_mismatch")


def _counts(record: dict, journaled: int, previous: VerificationCounts | None,
            operation: str) -> VerificationCounts:
    _require(all(name in record for name in _COUNT_NAMES) and "request_count" in record,
             "invalid_counts")
    values = tuple(record[name] for name in _COUNT_NAMES)
    a, p, v, j = values
    _require(_integer(j) and j == journaled, "invalid_counts")
    standalone = all(value is None for value in values[:3])
    if not standalone:
        _require(all(_integer(value) for value in values) and j <= v <= p <= a,
                 "invalid_counts")
        if operation in {"start", "page", "complete"}:
            _require(all(value == j for value in values), "invalid_counts")
        else:
            _require(a <= j + 1, "invalid_failure_counts")
    _require(record["request_count"] is None if standalone else
             type(record["request_count"]) is int and record["request_count"] == a,
             "request_count_mismatch")
    semantics = (
        "Attempted application-level requests are unknown" if standalone else
        "Attempted application-level requests dispatched by the coordinator"
    )
    _require(record.get("request_count_semantics") == semantics, "count_semantics_mismatch")
    if previous:
        old = tuple(previous.to_dict().values())
        _require((old[0] is None) == standalone, "accounting_mode_changed")
        _require(all(before is None or after >= before for before, after in zip(old, values)),
                 "decreasing_counts")
    return VerificationCounts(*values)


def _observations(record: dict, *, prefix: str = "") -> tuple[bool, int | None]:
    present_name = prefix + "number_matched_present"
    value_name = prefix + "number_matched"
    _require(present_name in record and value_name in record, "invalid_number_matched")
    present, value = record[present_name], record[value_name]
    _require(type(present) is bool and (_integer(value) if present else value is None),
             "invalid_number_matched")
    return present, value


def _page_fields(record: dict, *, unvalidated: bool = False) -> None:
    _require(_integer(record.get("requested_offset")), "invalid_page_offset")
    limit, count = record.get("requested_limit"), record.get("number_returned")
    _require(_integer(limit) and 1 <= limit <= 10000, "invalid_page_limit")
    _require(_integer(count) and (unvalidated or count <= limit), "invalid_number_returned")
    _require(_digest(record.get("stored_artifact_sha256")), "invalid_digest")
    _require(type(record.get("feature_id_check_complete")) is bool, "invalid_id_completeness")
    _observations(record)


def _source(reader: _Reader, record: dict) -> tuple[set[str], bool, str]:
    artifact_relative = record.get("relative_artifact_path")
    metadata_relative = record.get("relative_metadata_path")
    artifact, content = reader.document(artifact_relative)
    metadata, _ = reader.document(metadata_relative)
    digest = hashlib.sha256(content).hexdigest()
    _require(digest == record["stored_artifact_sha256"] == metadata.get("stored_artifact_sha256"),
             "stored_hash_mismatch")
    _require(_integer(metadata.get("stored_artifact_byte_count")) and
             metadata["stored_artifact_byte_count"] == len(content), "stored_byte_count_mismatch")
    _require(metadata.get("relative_stored_artifact_path") == artifact_relative,
             "artifact_path_mismatch")
    retrieved = _timestamp(metadata.get("retrieved_at_utc"))
    stem = (f"{retrieved.strftime('%Y%m%dT%H%M%S%fZ')}__pv_idn-94"
            f"__limit-{record['requested_limit']}__offset-{record['requested_offset']}"
            f"__sha256-{digest[:12]}")
    _require(Path(artifact_relative).name == stem + ".sanitized.json" and
             Path(metadata_relative).name == stem + ".metadata.json" and
             Path(artifact_relative).parent == Path(metadata_relative).parent,
             "artifact_filename_mismatch")
    _require(metadata.get("request_parameters") == {
        "pv_idn": "94", "limit": record["requested_limit"], "offset": record["requested_offset"],
    }, "request_parameters_mismatch")
    parameters = metadata["request_parameters"]
    _require(type(parameters["limit"]) is int and type(parameters["offset"]) is int,
             "request_parameters_mismatch")
    _require(metadata.get("metadata_schema_version") == "1.0" and
             metadata.get("sanitization_schema_version") == "1.0" and
             metadata.get("artifact_type") == "sanitized_source_response" and
             metadata.get("sanitization_applied") is True and
             metadata.get("original_response_persisted") is False and
             metadata.get("original_response_disposition") ==
             "The original response body was not persisted" and
             metadata.get("provider") == "GISTDA" and
             metadata.get("dataset") == "Historical Flood Recurrence" and
             metadata.get("endpoint_path") == "/features/flood-freq" and
             type(metadata.get("http_status")) is int and metadata["http_status"] == 200 and
             "content_type" in metadata and
             (metadata["content_type"] is None or isinstance(metadata["content_type"], str)),
             "invalid_provenance")
    _require(_digest(metadata.get("original_response_sha256")) and
             _integer(metadata.get("original_response_byte_count")) and
             _integer(metadata.get("removed_credential_count")), "invalid_provenance")
    removed_name_count = 0
    for name in ("removed_credential_field_names", "removed_credential_query_parameter_names"):
        names = metadata.get(name)
        supported = {"api_key", "api-key"} if name == "removed_credential_field_names" else {"api_key"}
        _require(isinstance(names, list) and all(
            isinstance(item, str) and item.casefold() in supported for item in names
        ) and len(names) == len(set(names)),
                 "invalid_provenance")
        removed_name_count += len(names)
    _require(metadata["removed_credential_count"] >= removed_name_count, "invalid_provenance")
    features = artifact.get("features")
    count = artifact.get("numberReturned")
    _require(artifact.get("type") == "FeatureCollection" and isinstance(features, list) and
             all(isinstance(feature, dict) for feature in features) and
             _integer(count) and count == len(features), "invalid_source_structure")
    present = "numberMatched" in artifact
    matched = artifact.get("numberMatched")
    _require(_integer(matched) if present else matched is None, "invalid_source_structure")
    ids = [feature.get("id") for feature in features]
    usable = [value for value in ids if isinstance(value, str) and value != ""]
    complete = len(usable) == len(ids)
    _require(count == record["number_returned"] and
             _integer(metadata.get("observed_number_returned")) and
             metadata["observed_number_returned"] == count and
             (present, matched) == _observations(record) ==
             _observations(metadata, prefix="observed_") and
             type(metadata.get("top_level_feature_id_check_complete")) is bool and
             metadata["top_level_feature_id_check_complete"] == complete ==
             record["feature_id_check_complete"], "source_observations_mismatch")
    fingerprint = hashlib.sha256(json.dumps(
        features, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()
    return set(usable), len(usable) != len(set(usable)), fingerprint


def verify_run(output_root: str | Path, run_id: str, *,
               api_key: str | None = None) -> RunVerificationReport:
    """Inspect an inactive run without changing files or loading configuration.

    Without api_key, credential-name checks still run but configured-key absence is
    explicitly unverified. Key-check completeness covers referenced published JSON,
    not unrecorded artifacts or temporary contents. Temporary warnings preserve a
    valid terminal status. Original-response bytes cannot be verified independently.
Only the last fully verified published snapshot is returned on invalid input.
"""
    safe_id = None
    counts = None
    page_count = 0
    total = 0
    observation = None
    terminal_kind = None
    temporary_count = unexpected_count = 0
    warnings: list[str] = []

    def report(status: str, issue: str | None = None) -> RunVerificationReport:
        issues = set(warnings)
        if issue:
            issues.add(issue)
        if api_key is None:
            issues.add("configured_key_unverified")
        return RunVerificationReport(
            safe_id, status, terminal_kind if status in {"complete", "failed"} else None,
            page_count, counts, status in {"complete", "failed"}, tuple(sorted(issues)),
            temporary_count, unexpected_count,
            api_key is not None and status in {"complete", "failed", "interrupted"},
            total if counts else None, observation[0] if observation else None,
            observation[1] if observation else None,
        )

    try:
        if api_key is not None:
            _require(isinstance(api_key, str) and bool(api_key.strip()), "invalid_api_key")
            try:
                api_key.encode("utf-8")
            except UnicodeError:
                raise _Invalid("invalid_api_key") from None
        _require(isinstance(run_id, str) and len(run_id) <= 128 and
                 re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is not None and
                 not run_id.endswith(".") and run_id.split(".")[0].upper() not in _RESERVED,
                 "invalid_run_id")
        _text(run_id, api_key)
        safe_id = run_id
        root = Path(output_root).absolute()
        # Reject root/ancestor symlinks too, before resolving any child reference.
        for ancestor in reversed((root, *root.parents)):
            try:
                _require(not _link(ancestor.lstat()), "symlink_rejected")
            except FileNotFoundError:
                return report("not_found")
        root = root.resolve()
        reader = _Reader(root, api_key)
        relative_directory = (_RUNS / run_id).as_posix()
        try:
            directory = reader.contained(relative_directory, directory=True)
        except FileNotFoundError:
            return report("not_found")
        names: set[str] = set()
        pages: list[tuple[int, str]] = []
        for entry in directory.iterdir():
            name = entry.name
            _text(name, api_key)
            names.add(name)
            if re.fullmatch(r"\.(?:run_started|run_complete|run_failed|page_\d+)\.json\..+\.tmp", name) and stat.S_ISREG(entry.lstat().st_mode):
                temporary_count += 1
                warnings.append("temporary_remnants")
                continue
            if name in {"run_started.json", "run_complete.json", "run_failed.json"}:
                continue
            match = re.fullmatch(r"page_(\d+)\.json", name)
            if match:
                pages.append((int(match[1]), name))
            else:
                unexpected_count += 1
        _require(not unexpected_count, "unexpected_entries")
        _require("run_started.json" in names, "missing_start_record")
        _require(not {"run_complete.json", "run_failed.json"}.issubset(names),
                 "multiple_terminal_records")
        start, _ = reader.document(f"{relative_directory}/run_started.json")
        _version(start, run_id)
        _require(start.get("run_id") == run_id and start.get("status") == "started" and
                 start.get("writer_model") == "single_writer_per_run", "invalid_start_record")
        _require(start.get("pagination_policy") == _POLICY, "unsupported_pagination_policy")
        started = _timestamp(start.get("started_at_utc"))
        offsets = start.get("expected_offsets")
        _require(isinstance(offsets, list) and bool(offsets) and
                 all(_integer(value) for value in offsets) and offsets[0] == 0 and
                 all(a < b for a, b in zip(offsets, offsets[1:])), "invalid_expected_offsets")
        counts = _counts(start, 0, None, "start")
        previous = None
        seen_ids: set[str] = set()
        seen_pages: set[str] = set()
        id_complete = True
        for index, (filename_index, name) in enumerate(sorted(pages)):
            _require(filename_index == index and name == f"page_{index:06d}.json",
                     "invalid_page_filename")
            page, _ = reader.document(f"{relative_directory}/{name}")
            _version(page, run_id)
            _require(type(page.get("page_index")) is int and page["page_index"] == index,
                     "invalid_page_index")
            _page_fields(page)
            _require(index < len(offsets) and page["requested_offset"] == offsets[index],
                     "offset_sequence_mismatch")
            if previous:
                _require(previous["number_returned"] == previous["requested_limit"],
                         "page_after_stop_page")
                _require(page["requested_offset"] == previous["requested_offset"] +
                         previous["requested_limit"], "offset_progression_mismatch")
                _require(_observations(page) == observation, "number_matched_changed")
            new_counts = _counts(page, index + 1, counts, "page")
            ids, duplicates, fingerprint = _source(reader, page)
            _require(fingerprint not in seen_pages, "repeated_page_content")
            _require(not duplicates and not seen_ids.intersection(ids), "duplicate_feature_ids")
            seen_pages.add(fingerprint)
            seen_ids.update(ids)
            id_complete = id_complete and page["feature_id_check_complete"]
            previous = page
            observation = _observations(page)
            total += page["number_returned"]
            page_count += 1
            counts = new_counts
        terminal_name = next((name for name in ("run_complete.json", "run_failed.json")
                              if name in names), None)
        if terminal_name is None:
            return report("interrupted")
        terminal, _ = reader.document(f"{relative_directory}/{terminal_name}")
        _version(terminal, run_id)
        kind = "complete" if terminal_name == "run_complete.json" else "failed"
        _require(terminal.get("run_id") == run_id and terminal.get("status") == kind,
                 "invalid_terminal_record")
        _require(_timestamp(terminal.get("started_at_utc")) == started and
                 _timestamp(terminal.get("completed_at_utc" if kind == "complete" else
                                        "failed_at_utc")) >= started, "terminal_timestamp_mismatch")
        new_counts = _counts(terminal, page_count, counts, kind)
        if kind == "complete":
            _require("unjournaled_page" not in terminal, "invalid_unjournaled_lineage")
            _require(previous is not None, "completion_requires_pages")
            _require(_integer(terminal.get("page_count")) and terminal["page_count"] == page_count and
                     _integer(terminal.get("total_features")) and terminal["total_features"] == total,
                     "completion_summary_mismatch")
            stop, returned, limit = terminal.get("stop_reason"), previous["number_returned"], previous["requested_limit"]
            _require((stop == "empty_page" and returned == 0) or
                     (stop == "partial_page" and 0 < returned < limit), "stop_reason_mismatch")
            _require(_observations(terminal, prefix="observed_") == observation,
                     "number_matched_summary_mismatch")
            _require(type(terminal.get("duplicate_id_check_complete")) is bool and
                     (not terminal["duplicate_id_check_complete"] or id_complete),
                     "id_completeness_summary_mismatch")
            _require(terminal.get("pagination_policy") == _POLICY and
                     terminal.get("pagination_policy_disclaimer") == _DISCLAIMER,
                     "unsupported_pagination_policy")
        else:
            category = terminal.get("failure_category")
            _require(isinstance(category, str) and
                     re.fullmatch(r"[a-z][a-z0-9_]*", category) is not None, "invalid_failure_category")
            last_offset = previous["requested_offset"] if previous else None
            _require(_integer(terminal.get("successful_page_count")) and
                     terminal["successful_page_count"] == page_count and
                     "last_successful_offset" in terminal and
                     terminal["last_successful_offset"] == last_offset and
                     (last_offset is None or type(terminal["last_successful_offset"]) is int) and
                     terminal.get("successful_page_count_semantics") ==
                     "Successfully published journal page records", "failure_summary_mismatch")
            reference = terminal.get("unjournaled_page")
            required = new_counts.persisted_page_count is not None and new_counts.persisted_page_count == page_count + 1
            _require(isinstance(reference, dict) if required else "unjournaled_page" not in terminal,
                     "invalid_unjournaled_lineage")
            if required:
                _require(set(reference) == {
                    "outcome_stage", "requested_offset", "requested_limit",
                    "relative_artifact_path", "relative_metadata_path",
                    "stored_artifact_sha256", "number_returned",
                    "number_matched_present", "number_matched", "feature_id_check_complete",
                }, "invalid_unjournaled_lineage")
                validated = new_counts.validated_page_count == page_count + 1
                _require(reference.get("outcome_stage") == (
                    "validated_unjournaled" if validated else "persisted_unvalidated"
                ), "invalid_unjournaled_lineage")
                _page_fields(reference, unvalidated=not validated)
                _require(page_count < len(offsets) and reference["requested_offset"] == offsets[page_count],
                         "offset_sequence_mismatch")
                if previous:
                    _require(previous["number_returned"] == previous["requested_limit"] and
                             reference["requested_offset"] == last_offset + previous["requested_limit"],
                             "offset_progression_mismatch")
                    if validated:
                        _require(_observations(reference) == observation, "number_matched_changed")
                ids, duplicates, fingerprint = _source(reader, reference)
                if validated:
                    _require(fingerprint not in seen_pages, "repeated_page_content")
                    _require(not duplicates and not seen_ids.intersection(ids), "duplicate_feature_ids")
        counts = new_counts
        terminal_kind = kind
        return report(kind)
    except _Invalid as error:
        return report("invalid", error.args[0])
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return report("invalid", "invalid_document")
    except (OSError, RuntimeError):
        return report("invalid", "filesystem_error")
