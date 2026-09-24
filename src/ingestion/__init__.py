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
from .pagination import PaginationPolicyError, PaginationRunError, PaginationRunResult, paginate_pattani
from .run_manifest import (
    PageJournalRecord, RunCounts, RunJournal, RunJournalError, UnjournaledPageReference,
)
from .infrastructure_acquisition import (
    AcquisitionError,
    AcquisitionResult,
    ApprovedResourceSpec,
    ArchiveLimits,
    CandidateResourceEvidence,
    GEOFABRIK_THAILAND_PBF_SPEC,
    MetadataRevalidationResult,
    acquire_resource,
    revalidate_selected_metadata,
)

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
    "PaginationRunError",
    "PaginationRunResult",
    "paginate_pattani",
    "PageJournalRecord",
    "RunJournal",
    "RunCounts",
    "RunJournalError",
    "UnjournaledPageReference",
    "AcquisitionError",
    "AcquisitionResult",
    "ApprovedResourceSpec",
    "ArchiveLimits",
    "CandidateResourceEvidence",
    "GEOFABRIK_THAILAND_PBF_SPEC",
    "MetadataRevalidationResult",
    "acquire_resource",
    "revalidate_selected_metadata",
]
