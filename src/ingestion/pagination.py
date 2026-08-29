"""Conservative mocked policy for coordinating Pattani source pages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.configuration import GistdaConfig
from src.ingestion.flood_frequency import SourceIngestionResult, ingest_pattani_page
from src.ingestion.gistda_client import GistdaClient


class PaginationPolicyError(RuntimeError):
    """Raised when conservative pagination safety policy is violated."""


@dataclass(frozen=True)
class PaginationRunResult:
    """Credential-safe in-memory summary; no run manifest is persisted."""

    pages: tuple[SourceIngestionResult, ...]
    requested_offsets: tuple[int, ...]
    stop_reason: str
    total_features: int
    observed_number_matched: int | None
    duplicate_id_check_complete: bool
    missing_feature_id_count: int

    def to_dict(self) -> dict[str, object]:
        """Return a sanitized machine-readable run summary."""

        return {
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


def paginate_pattani(
    *,
    config: GistdaConfig,
    client: GistdaClient,
    output_root: str | Path,
    limit: int,
    max_pages: int,
    retrieved_at: datetime | None = None,
) -> PaginationRunResult:
    """Apply offset pagination policy pending live behavioral verification.

    Response links are never followed. Offsets progress by the requested limit;
    empty and partial pages are candidate implementation stop rules, not claims
    about the official GISTDA contract.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValueError("limit must be an integer from 1 through 10000")
    if (
        isinstance(max_pages, bool)
        or not isinstance(max_pages, int)
        or max_pages <= 0
    ):
        raise ValueError("max_pages must be a positive integer")

    pages: list[SourceIngestionResult] = []
    requested_offsets: list[int] = []
    seen_page_content: set[str] = set()
    seen_feature_ids: set[str] = set()
    observed_number_matched: int | None = None
    number_matched_presence: bool | None = None
    missing_feature_id_count = 0
    offset = 0

    for _ in range(max_pages):
        page = ingest_pattani_page(
            config=config,
            client=client,
            output_root=output_root,
            limit=limit,
            offset=offset,
            retrieved_at=retrieved_at,
        )
        requested_offsets.append(offset)

        if page.page_content_sha256 in seen_page_content:
            raise PaginationPolicyError("repeated page content detected")
        seen_page_content.add(page.page_content_sha256)

        page_ids = set(page.feature_ids)
        if len(page_ids) != len(page.feature_ids) or page_ids & seen_feature_ids:
            raise PaginationPolicyError("duplicate top-level feature ID detected")
        seen_feature_ids.update(page_ids)

        if number_matched_presence is None:
            number_matched_presence = page.number_matched_present
            observed_number_matched = page.number_matched
        elif page.number_matched_present != number_matched_presence:
            raise PaginationPolicyError(
                "observed numberMatched presence changed across pages"
            )
        elif page.number_matched_present:
            if page.number_matched != observed_number_matched:
                raise PaginationPolicyError("observed numberMatched changed across pages")

        pages.append(page)
        missing_feature_id_count += page.missing_feature_id_count

        if page.number_returned == 0:
            stop_reason = "empty_page"
            break
        if page.number_returned < limit:
            stop_reason = "partial_page"
            break
        offset += limit
    else:
        raise PaginationPolicyError("maximum page safety cap reached")

    return PaginationRunResult(
        pages=tuple(pages),
        requested_offsets=tuple(requested_offsets),
        stop_reason=stop_reason,
        total_features=sum(page.number_returned for page in pages),
        observed_number_matched=observed_number_matched,
        duplicate_id_check_complete=missing_feature_id_count == 0,
        missing_feature_id_count=missing_feature_id_count,
    )
