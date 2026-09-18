"""Conservative mocked policy for coordinating Pattani source pages."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Final

from src.configuration import GistdaConfig, OFFICIAL_GISTDA_API_BASE_URL
from src.ingestion.flood_frequency import (
    PATTANI_PROVINCE_ID, SourceIngestionResult, SourcePageStructureError,
    SourceSanitizationError, ingest_pattani_page,
)
from src.ingestion.gistda_client import (
    GistdaClient, GistdaConnectionError, GistdaHTTPError, GistdaResponse, GistdaTimeoutError,
)
from src.ingestion.run_manifest import (
    RUNS_SUBDIRECTORY, PageJournalRecord, RunCounts, RunJournal, RunJournalError,
    UnjournaledPageReference,
)


# Internal allocation policy, not an official GISTDA pagination constraint.
MAX_JOURNALED_PAGES: Final = 100_000


class PaginationPolicyError(RuntimeError):
    """Raised when conservative pagination safety policy is violated."""

    def __init__(self, message: str, *, failure_category: str = "unexpected_failure") -> None:
        self.failure_category = failure_category
        super().__init__(message)


class PaginationRunError(PaginationPolicyError):
    """Safe outcome only; original exceptions and response data are not retained."""

    def __init__(self, *, failure_category: str, run_status: str, counts: RunCounts,
                 run_directory: Path | None = None, terminal_path: Path | None = None,
                 terminal_failure_category: str | None = None,
                 record_published: bool = False, cleanup_failed: bool = False,
                 unjournaled_page: UnjournaledPageReference | None = None) -> None:
        self.run_status = run_status
        self.counts = counts
        self.run_directory = run_directory
        self.terminal_path = terminal_path
        self.terminal_failure_category = terminal_failure_category
        self.record_published = record_published
        self.cleanup_failed = cleanup_failed
        self.unjournaled_page = unjournaled_page
        super().__init__(f"pagination run {failure_category}; status {run_status}",
                         failure_category=failure_category)


@dataclass(frozen=True)
class PaginationRunResult:
    """Safe summary with optional immutable journal references, never feature IDs."""

    pages: tuple[SourceIngestionResult, ...] = field(repr=False)
    requested_offsets: tuple[int, ...]
    stop_reason: str
    total_features: int
    observed_number_matched: int | None
    duplicate_id_check_complete: bool
    missing_feature_id_count: int
    run_id: str | None = field(default=None, repr=False)
    run_directory: Path | None = field(default=None, repr=False)
    terminal_path: Path | None = field(default=None, repr=False)
    counts: RunCounts | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a sanitized machine-readable run summary."""

        summary = {
            "requested_offsets": list(self.requested_offsets),
            "stop_reason": self.stop_reason,
            "page_count": len(self.pages),
            "total_features": self.total_features,
            "observed_number_matched": self.observed_number_matched,
            "duplicate_id_check_complete": self.duplicate_id_check_complete,
            "missing_feature_id_count": self.missing_feature_id_count,
            "pages": [
                {
                    "artifact_path": page.artifact_path.as_posix(),
                    "metadata_path": page.metadata_path.as_posix(),
                    "stored_artifact_sha256": page.stored_artifact_sha256,
                    "number_returned": page.number_returned,
                    "number_matched": page.number_matched,
                    "number_matched_present": page.number_matched_present,
                }
                for page in self.pages
            ],
        }
        if self.run_id is not None:
            summary.update({
                "run_id": self.run_id,
                "run_directory": self.run_directory.as_posix(),
                "terminal_path": self.terminal_path.as_posix(),
                "counts": self.counts.to_dict(),
            })
        return summary


class _CountingClient:
    """Count immediately at client-method dispatch; no HTTP logic or retries."""

    def __init__(self, client: GistdaClient) -> None:
        self._client = client
        self.attempted_request_count = 0

    def get_flood_frequency(self, **kwargs) -> GistdaResponse:
        self.attempted_request_count += 1
        return self._client.get_flood_frequency(**kwargs)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def paginate_pattani(
    *,
    config: GistdaConfig,
    client: GistdaClient,
    output_root: str | Path,
    limit: int,
    max_pages: int,
    retrieved_at: datetime | None = None,
    run_id: str | None = None,
    clock: Callable[[], datetime] | None = None,
) -> PaginationRunResult:
    """Apply offset pagination policy pending live behavioral verification.

    Response links are never followed. Offsets progress by the requested limit;
    empty and partial pages are candidate implementation stop rules, not claims
    about the official GISTDA contract. Journaling is opt-in, single-writer,
    and does not support resume. Source artifacts survive later run failures.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValueError("limit must be an integer from 1 through 10000")
    if (
        isinstance(max_pages, bool)
        or not isinstance(max_pages, int)
        or max_pages <= 0
    ):
        raise ValueError("max_pages must be a positive integer")
    journaled = run_id is not None
    if journaled:
        if max_pages > MAX_JOURNALED_PAGES:
            raise ValueError("journaled max_pages exceeds internal allocation policy")
        if config.province_id != PATTANI_PROVINCE_ID:
            raise ValueError("configured province ID does not match project Pattani scope")
        if config.api_base_url != OFFICIAL_GISTDA_API_BASE_URL:
            raise ValueError("configured base URL does not match the approved endpoint")
        if retrieved_at is not None and (
            not isinstance(retrieved_at, datetime) or retrieved_at.tzinfo is None
            or retrieved_at.utcoffset() is None
        ):
            raise ValueError("retrieved_at must be timezone-aware")
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")

    pages: list[SourceIngestionResult] = []
    requested_offsets: list[int] = []
    seen_page_content: set[str] = set()
    seen_feature_ids: set[str] = set()
    observed_number_matched: int | None = None
    number_matched_presence: bool | None = None
    missing_feature_id_count = 0
    offset = 0

    journal: RunJournal | None = None
    lifecycle_clock = clock if clock is not None else _utc_now
    counted = _CountingClient(client)
    persisted = validated = 0
    stage = "start"
    terminal_path = None
    pending_reference = None

    def snapshot() -> RunCounts:
        return RunCounts(counted.attempted_request_count, persisted, validated,
                         journal.page_count if journal is not None else 0)

    failure = None
    try:
        if journaled:
            expected_offsets = tuple(i * limit for i in range(max_pages))
            journal = RunJournal.start(
                output_root=output_root, run_id=run_id, expected_offsets=expected_offsets,
                started_at=lifecycle_clock(), api_key=config.api_key,
                counts=RunCounts(0, 0, 0, 0),
            )
        for _ in range(max_pages):
            stage = "source"
            page = ingest_pattani_page(
                config=config,
                client=counted if journaled else client,
                output_root=output_root,
                limit=limit,
                offset=offset,
                retrieved_at=retrieved_at,
            )
            persisted += 1
            stage = "validation"
            if journal is not None:
                page_record = PageJournalRecord(
                    page_index=journal.page_count, requested_offset=offset, requested_limit=limit,
                    artifact_path=page.artifact_path, metadata_path=page.metadata_path,
                    stored_artifact_sha256=page.stored_artifact_sha256,
                    number_returned=page.number_returned,
                    number_matched_present=page.number_matched_present,
                    number_matched=page.number_matched,
                    feature_id_check_complete=page.missing_feature_id_count == 0,
                )
                pending_reference = UnjournaledPageReference.from_page(
                    record=page_record, outcome_stage="persisted_unvalidated",
                    output_root=journal.output_root, api_key=config.api_key,
                )
            requested_offsets.append(offset)

            if journaled and not 0 <= page.number_returned <= limit:
                raise PaginationPolicyError("page count exceeds requested window",
                                            failure_category="page_count_invalid")
            if page.page_content_sha256 in seen_page_content:
                raise PaginationPolicyError("repeated page content detected",
                                            failure_category="repeated_page")
            seen_page_content.add(page.page_content_sha256)

            page_ids = set(page.feature_ids)
            if len(page_ids) != len(page.feature_ids) or page_ids & seen_feature_ids:
                raise PaginationPolicyError("duplicate top-level feature ID detected",
                                            failure_category="duplicate_feature_id")
            seen_feature_ids.update(page_ids)

            if number_matched_presence is None:
                number_matched_presence = page.number_matched_present
                observed_number_matched = page.number_matched
            elif page.number_matched_present != number_matched_presence:
                raise PaginationPolicyError("observed numberMatched presence changed across pages",
                                            failure_category="number_matched_changed")
            elif page.number_matched != observed_number_matched:
                raise PaginationPolicyError("observed numberMatched changed across pages",
                                            failure_category="number_matched_changed")

            validated += 1
            pages.append(page)
            missing_feature_id_count += page.missing_feature_id_count
            if journal is not None:
                stage = "append"
                pending_reference = replace(pending_reference, outcome_stage="validated_unjournaled")
                journal.append_page(page_record,
                                    counts=RunCounts(counted.attempted_request_count, persisted,
                                    validated, journal.page_count + 1))
                pending_reference = None

            if page.number_returned == 0:
                stop_reason = "empty_page"
                break
            if page.number_returned < limit:
                stop_reason = "partial_page"
                break
            offset += limit
        else:
            stage = "cap"
            raise PaginationPolicyError("maximum page safety cap reached",
                                        failure_category="max_pages_exhausted")
        if journal is not None:
            stage = "completion"
            terminal_path = journal.complete(
                completed_at=lifecycle_clock(), stop_reason=stop_reason,
                observed_number_matched=observed_number_matched,
                duplicate_id_check_complete=missing_feature_id_count == 0,
                counts=snapshot(),
            )
    except Exception as error:
        if not journaled:
            raise
        failure = _failed_outcome(stage, error, journal, snapshot(), lifecycle_clock,
                                  output_root=output_root, run_id=run_id,
                                  unjournaled_page=pending_reference)
    # Raise outside the handler so original exceptions are not retained as context.
    if failure is not None:
        raise failure

    return PaginationRunResult(
        pages=tuple(pages),
        requested_offsets=tuple(requested_offsets),
        stop_reason=stop_reason,
        total_features=sum(page.number_returned for page in pages),
        observed_number_matched=observed_number_matched,
        duplicate_id_check_complete=missing_feature_id_count == 0,
        missing_feature_id_count=missing_feature_id_count,
        run_id=journal.run_id if journal is not None else None,
        run_directory=journal.run_directory if journal is not None else None,
        terminal_path=terminal_path,
        counts=journal.counts if journal is not None else None,
    )


def _failure_category(stage: str, error: Exception) -> str:
    if stage == "start":
        return "journal_start_failed"
    if stage == "append":
        return ("journal_page_cleanup_failed" if isinstance(error, RunJournalError)
                and error.record_published else "journal_page_failed")
    if stage == "completion":
        return ("completion_cleanup_failed" if isinstance(error, RunJournalError)
                and error.record_published else "completion_terminal_failed")
    if isinstance(error, GistdaTimeoutError):
        return "request_timeout"
    if isinstance(error, GistdaConnectionError):
        return "request_connection_failed"
    if isinstance(error, GistdaHTTPError):
        return "request_http_failed"
    if isinstance(error, SourceSanitizationError):
        return "source_sanitization_failed"
    if isinstance(error, SourcePageStructureError):
        return "source_structure_invalid"
    if stage == "source" and isinstance(error, OSError):
        return "source_publication_failed"
    if isinstance(error, PaginationPolicyError) and error.failure_category in {
        "page_count_invalid", "repeated_page", "duplicate_feature_id",
        "number_matched_changed", "max_pages_exhausted",
    }:
        return error.failure_category
    return "unexpected_failure"


def _failed_outcome(stage: str, error: Exception, journal: RunJournal | None,
                    counts: RunCounts, clock: Callable[[], datetime], *,
                    output_root: str | Path, run_id: str,
                    unjournaled_page: UnjournaledPageReference | None) -> PaginationRunError:
    category = _failure_category(stage, error)
    published = isinstance(error, RunJournalError) and error.record_published
    cleanup = isinstance(error, RunJournalError) and error.cleanup_failed
    terminal_failure = None
    terminal_path = None
    directory = journal.run_directory if journal is not None else None
    # Publication callbacks may have advanced the journal before cleanup failed.
    # Never report a published page as unjournaled, nor infer source remnants.
    if counts.persisted_page_count == counts.journaled_page_count:
        unjournaled_page = None
    if journal is None:
        status = "interrupted" if published else "not_started"
        if published:
            # Start already validated these inputs and published its disk record.
            directory = Path(output_root).resolve() / RUNS_SUBDIRECTORY / run_id
    else:
        status = journal.status if journal.is_terminal else "interrupted"
        if journal.is_terminal:
            terminal_path = journal.run_directory / f"run_{journal.status}.json"
        elif not journal.cleanup_failed:
            try:
                terminal_path = journal.fail(failed_at=clock(), failure_category=category,
                                             counts=counts, unjournaled_page=unjournaled_page)
                status = "failed"
            except Exception as terminal_error:
                terminal_published = (isinstance(terminal_error, RunJournalError)
                                      and terminal_error.record_published)
                terminal_failure = ("failure_terminal_cleanup_failed" if terminal_published
                                    else "failure_terminal_failed")
                cleanup |= (isinstance(terminal_error, RunJournalError)
                            and terminal_error.cleanup_failed)
                published |= terminal_published
                if journal.is_terminal:
                    status = journal.status
                    terminal_path = journal.run_directory / f"run_{journal.status}.json"
                else:
                    status = "interrupted"
        cleanup |= journal.cleanup_failed
    return PaginationRunError(
        failure_category=category, terminal_failure_category=terminal_failure,
        run_status=status, counts=counts,
        run_directory=directory,
        terminal_path=terminal_path, record_published=published, cleanup_failed=cleanup,
        unjournaled_page=unjournaled_page,
    )
