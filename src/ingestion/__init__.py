"""GISTDA ingestion interfaces."""

from .gistda_client import (
    GistdaClient,
    GistdaClientError,
    GistdaConnectionError,
    GistdaHTTPError,
    GistdaResponse,
    GistdaTimeoutError,
)
from .flood_frequency import (
    SourcePageStructureError,
    SourceIngestionResult,
    SourceSanitizationError,
    ingest_pattani_page,
    ingest_pattani_sample,
)
from .pagination import PaginationPolicyError, PaginationRunResult, paginate_pattani
from .run_manifest import PageJournalRecord, RunJournal, RunJournalError

__all__ = [
    "GistdaClient",
    "GistdaClientError",
    "GistdaConnectionError",
    "GistdaHTTPError",
    "GistdaResponse",
    "GistdaTimeoutError",
    "SourceIngestionResult",
    "SourcePageStructureError",
    "SourceSanitizationError",
    "ingest_pattani_page",
    "ingest_pattani_sample",
    "PaginationPolicyError",
    "PaginationRunResult",
    "paginate_pattani",
    "PageJournalRecord",
    "RunJournal",
    "RunJournalError",
]
