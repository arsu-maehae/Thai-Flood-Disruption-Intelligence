"""GISTDA ingestion interfaces."""

from .gistda_client import (
    GistdaClient,
    GistdaClientError,
    GistdaConnectionError,
    GistdaHTTPError,
    GistdaResponse,
    GistdaTimeoutError,
)
from .flood_frequency import RawIngestionResult, ingest_pattani_sample

__all__ = [
    "GistdaClient",
    "GistdaClientError",
    "GistdaConnectionError",
    "GistdaHTTPError",
    "GistdaResponse",
    "GistdaTimeoutError",
    "RawIngestionResult",
    "ingest_pattani_sample",
]
