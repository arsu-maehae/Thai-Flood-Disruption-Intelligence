"""Local PostGIS-backed aggregate exposure store."""

from .exposure_store import (
    DB_SCHEMA_VERSION,
    AggregateSnapshot,
    ExposureStore,
    LoadResult,
    StoreError,
)

__all__ = [
    "DB_SCHEMA_VERSION",
    "AggregateSnapshot",
    "ExposureStore",
    "LoadResult",
    "StoreError",
]
