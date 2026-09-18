"""Immutable journal for one writer per run; no concurrency or resume support.

Each hard-link publication is atomic, not transactional for the entire run.
A linked record remains final even if removal of its temporary name fails.
Standalone request counts remain unknown; coordinators supply explicit snapshots.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Callable, Final, Sequence
from urllib.parse import parse_qsl, unquote_plus, urlsplit

JOURNAL_SCHEMA_VERSION: Final = "1.2"
PIPELINE_POLICY_VERSION: Final = "1.0"
DEFAULT_PAGINATION_POLICY: Final = "observed_offset_limit_v1"
POLICY_DISCLAIMER: Final = (
    "Ordering and termination are observed implementation policy, not an "
    "official GISTDA contract."
)
RUNS_SUBDIRECTORY: Final = Path("gistda/flood_freq/pattani/runs")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SAFE_CATEGORY = re.compile(r"[a-z][a-z0-9_]*\Z")
_CREDENTIAL_KEYS = frozenset({"api_key", "api-key", "authorization"})
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in range(1, 10)}
)


class RunJournalError(RuntimeError):
    """Safe failure with explicit publication and cleanup outcome flags."""

    def __init__(self, category: str, *, record_published: bool = False,
                 cleanup_failed: bool = False) -> None:
        # Internal callers supply fixed category literals, never exception text.
        self.record_published = record_published
        self.cleanup_failed = cleanup_failed
        self.failure_category = category
        super().__init__(f"run journal {category}")


@dataclass(frozen=True)
class RunCounts:
    """Cumulative outcomes; standalone journals use three unknown counts."""

    attempted_request_count: int | None
    persisted_page_count: int | None
    validated_page_count: int | None
    journaled_page_count: int

    def __post_init__(self) -> None:
        if not _nonnegative_integer(self.journaled_page_count):
            raise ValueError("journaled count must be a non-negative integer")
        values = (self.attempted_request_count, self.persisted_page_count,
                  self.validated_page_count)
        if all(v is None for v in values):
            return
        if any(not _nonnegative_integer(v) for v in values):
            raise ValueError("known counts must be non-negative integers")
        if not self.journaled_page_count <= values[2] <= values[1] <= values[0]:
            raise ValueError("counts must satisfy journaled <= validated <= persisted <= attempted")

    def to_dict(self) -> dict[str, int | None]:
        return {
            "attempted_request_count": self.attempted_request_count,
            "persisted_page_count": self.persisted_page_count,
            "validated_page_count": self.validated_page_count,
            "journaled_page_count": self.journaled_page_count,
        }


@dataclass(frozen=True, repr=False)
class PageJournalRecord:
    page_index: int
    requested_offset: int
    requested_limit: int
    artifact_path: Path
    metadata_path: Path
    stored_artifact_sha256: str
    number_returned: int
    number_matched_present: bool
    number_matched: int | None
    feature_id_check_complete: bool

    def __repr__(self) -> str:
        return "PageJournalRecord()"


@dataclass(frozen=True, repr=False)
class UnjournaledPageReference:
    """Immutable lineage only; no response content, feature IDs, or credentials."""

    outcome_stage: str
    requested_offset: int
    requested_limit: int
    relative_artifact_path: str
    relative_metadata_path: str
    stored_artifact_sha256: str
    number_returned: int
    number_matched_present: bool
    number_matched: int | None
    feature_id_check_complete: bool

    def __repr__(self) -> str:
        return "UnjournaledPageReference()"

    def __post_init__(self) -> None:
        if not isinstance(self.outcome_stage, str) or self.outcome_stage not in {
            "persisted_unvalidated", "validated_unjournaled",
        }:
            raise ValueError("invalid unjournaled page outcome stage")
        # An unvalidated persisted page may exceed its requested window.
        # Validate all other observed fields using the existing journal rules.
        _validate_page_record(PageJournalRecord(
            0, self.requested_offset, self.requested_limit, Path(), Path(),
            self.stored_artifact_sha256, 0, self.number_matched_present,
            self.number_matched, self.feature_id_check_complete,
        ))
        if not _nonnegative_integer(self.number_returned):
            raise ValueError("referenced count must be a non-negative integer")
        for value in (self.relative_artifact_path, self.relative_metadata_path):
            if (not isinstance(value, str) or not value or Path(value).is_absolute()
                    or PureWindowsPath(value).drive or PureWindowsPath(value).root
                    or not Path(value).parts or ".." in PureWindowsPath(value).parts
                    or ".." in Path(value).parts or value in {".", "./"}):
                raise ValueError("reference paths must be relative beneath output_root")

    @classmethod
    def from_page(cls, *, record: PageJournalRecord, outcome_stage: str,
                  output_root: str | Path, api_key: str) -> "UnjournaledPageReference":
        if not isinstance(record, PageJournalRecord):
            raise ValueError("a PageJournalRecord is required for lineage")
        root = Path(output_root).resolve()
        reference = cls(
            outcome_stage, record.requested_offset, record.requested_limit,
            _relative_beneath(record.artifact_path, root),
            _relative_beneath(record.metadata_path, root), record.stored_artifact_sha256,
            record.number_returned, record.number_matched_present, record.number_matched,
            record.feature_id_check_complete,
        )
        reference.verified_dict(output_root=root, api_key=api_key)
        return reference

    def to_dict(self) -> dict[str, object]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def verified_dict(self, *, output_root: Path, api_key: str) -> dict[str, object]:
        """Recheck containment (including symlinks) and credential safety."""
        root = Path(output_root).resolve()
        content = self.to_dict()
        # Check supplied strings before filesystem normalization as well.
        _verify_value(content, api_key)
        for name in ("relative_artifact_path", "relative_metadata_path"):
            content[name] = _relative_beneath(root / content[name], root)
        _serialize_and_verify(content, api_key)
        return content


@dataclass(repr=False)
class RunJournal:
    """Append-only writer requiring a single writer per run.

    Terminal-file checks are defensive, not cross-process locks. A cleanup fault
    blocks this instance without changing the status of published disk records.
    """

    output_root: Path
    run_id: str
    expected_offsets: tuple[int, ...]
    started_at: datetime
    api_key: str = field(repr=False)
    pagination_policy: str = DEFAULT_PAGINATION_POLICY
    initial_counts: RunCounts | None = field(default=None, repr=False)
    run_directory: Path = field(init=False)
    _pages: list[PageJournalRecord] = field(default_factory=list, init=False, repr=False)
    _terminal: bool = field(default=False, init=False, repr=False)
    _cleanup_fault: bool = field(default=False, init=False, repr=False)
    _status: str = field(default="started", init=False, repr=False)
    _counts: RunCounts | None = field(default=None, init=False, repr=False)
    _coordinated: bool = field(default=False, init=False, repr=False)

    def __repr__(self) -> str:
        return "RunJournal()"

    def __post_init__(self) -> None:
        _validate_run_id(self.run_id)
        _validate_identifier(self.pagination_policy)
        self._coordinated = self.initial_counts is not None
        if self._coordinated and self.initial_counts != RunCounts(0, 0, 0, 0):
            raise ValueError("coordinated start counts must all be zero")
        snapshot = self._proposed_counts(self.initial_counts, 0, "start")
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("a non-empty credential is required for in-memory checks")
        try:
            self.api_key.encode("utf-8")
        except UnicodeError:
            raise ValueError("credential must be valid UTF-8 text") from None
        self.started_at = _normalize_utc(self.started_at)
        try:
            self.expected_offsets = tuple(self.expected_offsets)
        except TypeError:
            raise ValueError("expected_offsets must be an integer sequence") from None
        if (
            not self.expected_offsets
            or any(not _nonnegative_integer(v) for v in self.expected_offsets)
            or self.expected_offsets[0] != 0
            or any(a >= b for a, b in zip(self.expected_offsets, self.expected_offsets[1:]))
        ):
            raise ValueError("expected_offsets must start at zero and strictly increase")
        try:
            self.output_root = Path(self.output_root).resolve()
            self.run_directory = self.output_root / RUNS_SUBDIRECTORY / self.run_id
            _relative_beneath(self.run_directory, self.output_root)
        except (OSError, TypeError):
            raise ValueError("invalid journal output path") from None
        content = _serialize_and_verify(
            self._base_record() | {
                "run_id": self.run_id,
                "status": "started",
                "started_at_utc": _timestamp(self.started_at),
                "pagination_policy": self.pagination_policy,
                "expected_offsets": list(self.expected_offsets),
                "writer_model": "single_writer_per_run",
            } | self._count_fields(snapshot), self.api_key,
        )
        owned_directory = False
        try:
            self.run_directory.mkdir(parents=True, exist_ok=False)
            owned_directory = True
            self._publish("run_started.json", content, lambda: self._set_counts(snapshot))
        except FileExistsError:
            if owned_directory:
                _remove_owned_empty_directory(self.run_directory)
            raise FileExistsError("run journal destination already exists") from None
        except RunJournalError as error:
            if owned_directory and not error.record_published:
                _remove_owned_empty_directory(self.run_directory)
            raise
        except OSError:
            if owned_directory:
                _remove_owned_empty_directory(self.run_directory)
            raise RunJournalError("directory_creation_failed") from None

    @classmethod
    def start(cls, *, output_root: str | Path, run_id: str,
              expected_offsets: Sequence[int], started_at: datetime, api_key: str,
              pagination_policy: str = DEFAULT_PAGINATION_POLICY,
              counts: RunCounts | None = None) -> "RunJournal":
        return cls(output_root, run_id, expected_offsets, started_at, api_key,
                   pagination_policy, counts)

    @property
    def counts(self) -> RunCounts:
        """Latest successfully published snapshot, never a pending proposal."""
        if self._counts is None:
            raise RunJournalError("start_not_published")
        return self._counts

    @property
    def is_terminal(self) -> bool:
        return self._terminal

    @property
    def status(self) -> str:
        return self._status

    @property
    def cleanup_failed(self) -> bool:
        return self._cleanup_fault

    @property
    def page_count(self) -> int:
        """Count page records successfully published by this instance."""
        return len(self._pages)

    def append_page(self, record: PageJournalRecord, *, counts: RunCounts | None = None) -> Path:
        self._ensure_open()
        if not isinstance(record, PageJournalRecord):
            raise ValueError("a PageJournalRecord is required")
        _validate_page_record(record)
        # Inspect supplied paths before platform normalization changes separators.
        _verify_value(str(record.artifact_path), self.api_key)
        _verify_value(str(record.metadata_path), self.api_key)
        index = len(self._pages)
        if record.page_index != index:
            raise RunJournalError("non_contiguous_page_index")
        if index >= len(self.expected_offsets):
            raise RunJournalError("pagination_sequence_exhausted")
        if record.requested_offset != self.expected_offsets[index]:
            raise RunJournalError("offset_sequence_mismatch")
        if self._pages:
            previous = self._pages[-1]
            if previous.number_returned < previous.requested_limit:
                raise RunJournalError("page_after_stop_page")
            if record.requested_offset != previous.requested_offset + previous.requested_limit:
                raise RunJournalError("offset_progression_mismatch")
            if (record.number_matched_present, record.number_matched) != (
                previous.number_matched_present, previous.number_matched
            ):
                raise RunJournalError("number_matched_changed")
        snapshot = self._proposed_counts(counts, index + 1, "page")
        content = _serialize_and_verify(
            self._base_record() | {
                "page_index": record.page_index,
                "requested_offset": record.requested_offset,
                "requested_limit": record.requested_limit,
                "relative_artifact_path": _relative_beneath(record.artifact_path, self.output_root),
                "relative_metadata_path": _relative_beneath(record.metadata_path, self.output_root),
                "stored_artifact_sha256": record.stored_artifact_sha256,
                "number_returned": record.number_returned,
                "number_matched_present": record.number_matched_present,
                "number_matched": record.number_matched,
                "feature_id_check_complete": record.feature_id_check_complete,
            } | self._count_fields(snapshot), self.api_key,
        )
        return self._publish(f"page_{index:06d}.json", content,
                             lambda: self._publish_page(record, snapshot))

    def complete(self, *, completed_at: datetime, stop_reason: str,
                 observed_number_matched: int | None,
                 duplicate_id_check_complete: bool,
                 counts: RunCounts | None = None) -> Path:
        self._ensure_open()
        completed = self._terminal_timestamp(completed_at)
        _validate_identifier(stop_reason)
        if not self._pages:
            raise RunJournalError("completion_requires_page_records")
        last = self._pages[-1]
        valid_stop = (stop_reason == "empty_page" and last.number_returned == 0) or (
            stop_reason == "partial_page" and 0 < last.number_returned < last.requested_limit
        )
        if not valid_stop:
            raise RunJournalError("stop_reason_mismatch")
        if observed_number_matched is not None and not _nonnegative_integer(observed_number_matched):
            raise ValueError("observed_number_matched must be null or a non-negative integer")
        if observed_number_matched != last.number_matched:
            raise RunJournalError("number_matched_summary_mismatch")
        if not isinstance(duplicate_id_check_complete, bool):
            raise ValueError("duplicate_id_check_complete must be boolean")
        if duplicate_id_check_complete and not all(p.feature_id_check_complete for p in self._pages):
            raise RunJournalError("id_completeness_summary_mismatch")
        snapshot = self._proposed_counts(counts, len(self._pages), "complete")
        content = _serialize_and_verify(
            self._base_record() | {
                "run_id": self.run_id,
                "status": "complete",
                "started_at_utc": _timestamp(self.started_at),
                "completed_at_utc": _timestamp(completed),
                "page_count": len(self._pages),
                "total_features": sum(p.number_returned for p in self._pages),
                "stop_reason": stop_reason,
                "observed_number_matched_present": last.number_matched_present,
                "observed_number_matched": observed_number_matched,
                "duplicate_id_check_complete": duplicate_id_check_complete,
                "pagination_policy": self.pagination_policy,
                "pagination_policy_disclaimer": POLICY_DISCLAIMER,
            } | self._count_fields(snapshot), self.api_key,
        )
        return self._publish("run_complete.json", content,
                             lambda: self._set_terminal("complete", snapshot))

    def fail(self, *, failed_at: datetime, failure_category: str,
             counts: RunCounts | None = None,
             unjournaled_page: UnjournaledPageReference | None = None) -> Path:
        self._ensure_open()
        failed = self._terminal_timestamp(failed_at)
        _validate_identifier(failure_category)
        snapshot = self._proposed_counts(counts, len(self._pages), "failed")
        reference = self._failure_reference(snapshot, unjournaled_page)
        content = _serialize_and_verify(
            self._base_record() | {
                "run_id": self.run_id,
                "status": "failed",
                "started_at_utc": _timestamp(self.started_at),
                "failed_at_utc": _timestamp(failed),
                "failure_category": failure_category,
                "successful_page_count": len(self._pages),
                "successful_page_count_semantics": "Successfully published journal page records",
                "last_successful_offset": self._pages[-1].requested_offset if self._pages else None,
            } | self._count_fields(snapshot) | (
                {"unjournaled_page": reference} if reference is not None else {}
            ), self.api_key,
        )
        return self._publish("run_failed.json", content,
                             lambda: self._set_terminal("failed", snapshot))

    def _failure_reference(self, counts: RunCounts,
                           reference: UnjournaledPageReference | None) -> dict[str, object] | None:
        if not self._coordinated:
            if reference is not None:
                raise ValueError("standalone accounting cannot accept an unjournaled reference")
            return None
        difference = counts.persisted_page_count - counts.journaled_page_count
        if difference == 0:
            if reference is not None:
                raise ValueError("unjournaled reference does not match persisted counts")
            return None
        if difference != 1 or not isinstance(reference, UnjournaledPageReference):
            raise ValueError("persisted unjournaled page requires a lineage reference")
        expected_stage = ("validated_unjournaled"
                          if counts.validated_page_count == counts.journaled_page_count + 1
                          else "persisted_unvalidated")
        if reference.outcome_stage != expected_stage:
            raise ValueError("lineage stage does not match validated counts")
        index = self.page_count
        if index >= len(self.expected_offsets) or reference.requested_offset != self.expected_offsets[index]:
            raise ValueError("lineage offset does not match pagination sequence")
        if self._pages and (
            self._pages[-1].number_returned < self._pages[-1].requested_limit
            or reference.requested_offset != self._pages[-1].requested_offset + self._pages[-1].requested_limit
        ):
            raise ValueError("lineage reference does not match pagination progression")
        if expected_stage == "validated_unjournaled":
            if reference.number_returned > reference.requested_limit:
                raise ValueError("validated lineage count exceeds requested window")
            if self._pages and (reference.number_matched_present, reference.number_matched) != (
                self._pages[-1].number_matched_present, self._pages[-1].number_matched
            ):
                raise ValueError("validated lineage total observation is inconsistent")
        return reference.verified_dict(output_root=self.output_root, api_key=self.api_key)

    def _set_counts(self, counts: RunCounts) -> None:
        self._counts = counts

    def _publish_page(self, record: PageJournalRecord, counts: RunCounts) -> None:
        self._pages.append(record)
        self._set_counts(counts)

    def _set_terminal(self, status: str, counts: RunCounts) -> None:
        self._set_counts(counts)
        self._status = status
        self._terminal = True

    def _proposed_counts(self, counts: RunCounts | None, journaled: int,
                         operation: str) -> RunCounts:
        if not self._coordinated:
            if counts is not None:
                raise ValueError("standalone accounting mode cannot change")
            return RunCounts(None, None, None, journaled)
        if not isinstance(counts, RunCounts) or counts.attempted_request_count is None:
            raise ValueError("coordinated accounting requires known RunCounts")
        if counts.journaled_page_count != journaled:
            raise ValueError("journaled count must match published records")
        if self._counts is not None and any(
            value < self._counts.to_dict()[key] for key, value in counts.to_dict().items()
        ):
            raise ValueError("count snapshots cannot decrease")
        if operation in {"start", "page", "complete"} and any(
            value != journaled for value in counts.to_dict().values()
        ):
            raise ValueError("successful publication counts must agree")
        if operation == "failed" and counts.attempted_request_count > journaled + 1:
            raise ValueError("failure count exceeds stop-on-first-error policy")
        return counts

    @staticmethod
    def _count_fields(counts: RunCounts) -> dict[str, object]:
        return counts.to_dict() | {
            "request_count": counts.attempted_request_count,
            "request_count_semantics": (
                "Attempted application-level requests are unknown"
                if counts.attempted_request_count is None else
                "Attempted application-level requests dispatched by the coordinator"
            ),
        }

    def _terminal_timestamp(self, value: datetime) -> datetime:
        timestamp = _normalize_utc(value)
        if timestamp < self.started_at:
            raise ValueError("terminal timestamp must not precede started_at")
        return timestamp

    @staticmethod
    def _base_record() -> dict[str, object]:
        return {"journal_schema_version": JOURNAL_SCHEMA_VERSION,
                "pipeline_policy_version": PIPELINE_POLICY_VERSION}

    def _ensure_open(self) -> None:
        if self._terminal:
            raise RunJournalError("terminal_state_is_final")
        if self._cleanup_fault:
            raise RunJournalError("instance_blocked_by_cleanup_fault", cleanup_failed=True)
        # Defensive only: concurrent writers could race this check.
        if any(os.path.lexists(self.run_directory / name) for name in (
            "run_complete.json", "run_failed.json"
        )):
            raise RunJournalError("terminal_record_already_exists")

    def _publish(self, name: str, content: bytes, on_published: Callable[[], None]) -> Path:
        destination = self.run_directory / name
        try:
            temporary = _write_temp(self.run_directory, name, content)
        except RunJournalError as error:
            self._cleanup_fault |= error.cleanup_failed
            raise
        try:
            os.link(temporary, destination)
        except OSError as error:
            cleaned = _remove_temp(temporary)
            self._cleanup_fault |= not cleaned
            if isinstance(error, FileExistsError) and cleaned:
                raise FileExistsError("run journal destination already exists") from None
            raise RunJournalError("publication_failed", cleanup_failed=not cleaned) from None
        # Synchronize final publication state before attempting temporary cleanup.
        on_published()
        if not _remove_temp(temporary):
            self._cleanup_fault = True
            raise RunJournalError("published_with_cleanup_fault",
                                  record_published=True, cleanup_failed=True) from None
        return destination


def _validate_run_id(value: str) -> None:
    if (not isinstance(value, str) or not _SAFE_ID.fullmatch(value)
            or len(value) > 128 or value.endswith(".")
            or value.split(".")[0].upper() in _WINDOWS_RESERVED):
        raise ValueError("run_id must be a non-empty filesystem-safe identifier")


def _validate_identifier(value: str) -> None:
    if not isinstance(value, str) or not _SAFE_CATEGORY.fullmatch(value):
        raise ValueError("a safe lowercase category identifier is required")


def _nonnegative_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_page_record(record: PageJournalRecord) -> None:
    if any(not _nonnegative_integer(v) for v in (
        record.page_index, record.requested_offset, record.number_returned
    )):
        raise ValueError("page index, offset and count must be non-negative integers")
    if not _nonnegative_integer(record.requested_limit) or not 1 <= record.requested_limit <= 10000:
        raise ValueError("requested_limit must be an integer from 1 through 10000")
    if record.number_returned > record.requested_limit:
        raise ValueError("number_returned must not exceed requested_limit")
    if not isinstance(record.stored_artifact_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", record.stored_artifact_sha256
    ):
        raise ValueError("stored_artifact_sha256 must be a SHA-256 hex digest")
    if not isinstance(record.number_matched_present, bool):
        raise ValueError("number_matched_present must be boolean")
    if record.number_matched_present:
        if not _nonnegative_integer(record.number_matched):
            raise ValueError("present number_matched must be a non-negative integer")
    elif record.number_matched is not None:
        raise ValueError("absent number_matched must be null")
    if not isinstance(record.feature_id_check_complete, bool):
        raise ValueError("feature_id_check_complete must be boolean")


def _relative_beneath(path: str | Path, root: Path) -> str:
    try:
        relative = Path(path).resolve().relative_to(root)
        if not relative.parts:
            raise ValueError
        return relative.as_posix()
    except (ValueError, TypeError, OSError):
        raise ValueError("journal paths must resolve beneath output_root") from None


def _normalize_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("journal timestamps must be timezone-aware datetimes")
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _serialize_and_verify(value: dict[str, object], api_key: str) -> bytes:
    _verify_value(value, api_key)
    try:
        key_bytes = api_key.encode("utf-8")
        content = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                              allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise RunJournalError("serialization_failed") from None
    if key_bytes in content:
        raise RunJournalError("credential_safety_verification_failed")
    return content


def _normalized_credential_name(value: str) -> str:
    """Normalize URL/form encodings at every layer before name comparison."""
    while True:
        decoded = unquote_plus(value)
        if decoded == value:
            return decoded.casefold()
        value = decoded


def _verify_value(value: object, api_key: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or _normalized_credential_name(key) in _CREDENTIAL_KEYS:
                raise RunJournalError("credential_field_rejected")
            _verify_value(key, api_key)
            _verify_value(child, api_key)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _verify_value(child, api_key)
    elif isinstance(value, str):
        decoded = value
        while True:
            if api_key in decoded or re.search(
                r"(?i)(?:api[_-]key|authorization)\s*:", decoded
            ):
                raise RunJournalError("credential_safety_verification_failed")
            try:
                if any(_normalized_credential_name(name) in _CREDENTIAL_KEYS for name, _ in parse_qsl(
                    urlsplit(decoded).query, keep_blank_values=True
                )):
                    raise RunJournalError("credential_query_rejected")
            except ValueError:
                raise RunJournalError("invalid_record_string") from None
            next_decoded = unquote_plus(decoded)
            if next_decoded == decoded:
                break
            decoded = next_decoded


def _remove_temp(path: str) -> bool:
    try:
        Path(path).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _remove_owned_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _write_temp(directory: Path, name: str, content: bytes) -> str:
    temporary = None
    path: str | None = None
    try:
        temporary = tempfile.NamedTemporaryFile(
            mode="wb", dir=directory, prefix=f".{name}.", suffix=".tmp", delete=False
        )
        path = temporary.name
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary.close()
        return path
    except Exception:
        if temporary is not None:
            try:
                temporary.close()
            except Exception:
                pass
        cleaned = path is None or _remove_temp(path)
        raise RunJournalError("temporary_write_failed", cleanup_failed=not cleaned) from None
