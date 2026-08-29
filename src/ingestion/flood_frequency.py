"""Single-page raw ingestion for GISTDA historical flood recurrence data."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from src.configuration import GistdaConfig
from src.ingestion.gistda_client import FLOOD_FREQUENCY_PATH, GistdaClient


SAMPLE_LIMIT: Final = 10
SAMPLE_OFFSET: Final = 0
METADATA_SCHEMA_VERSION: Final = "1.0"
PROVIDER: Final = "GISTDA"
DATASET: Final = "Historical Flood Recurrence"
RAW_SUBDIRECTORY: Final = Path("gistda/flood_freq/pattani")
# Project scope derived from previously observed behavior; this is not an
# officially documented mapping of province ID 94 to Pattani.
PATTANI_PROVINCE_ID: Final = "94"


@dataclass(frozen=True)
class RawIngestionResult:
    """Paths and integrity information for one controlled retrieval."""

    raw_path: Path
    metadata_path: Path
    sha256: str
    created: bool


def ingest_pattani_sample(
    *,
    config: GistdaConfig,
    client: GistdaClient,
    output_root: str | Path,
    retrieved_at: datetime | None = None,
) -> RawIngestionResult:
    """Retrieve one Pattani sample using the previously observed province ID."""

    if config.province_id != PATTANI_PROVINCE_ID:
        raise ValueError("configured province ID does not match project Pattani scope")

    timestamp = (
        _normalize_timestamp(retrieved_at) if retrieved_at is not None else None
    )
    response = client.get_flood_frequency(
        pv_idn=config.province_id,
        limit=SAMPLE_LIMIT,
        offset=SAMPLE_OFFSET,
    )
    if timestamp is None:
        timestamp = _normalize_timestamp(_utc_now())

    raw_bytes = response.content
    digest = hashlib.sha256(raw_bytes).hexdigest()
    timestamp_text = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    safe_province_id = _filesystem_safe(config.province_id)
    stem = (
        f"{timestamp_text}__pv_idn-{safe_province_id}"
        f"__limit-{SAMPLE_LIMIT}__offset-{SAMPLE_OFFSET}"
        f"__sha256-{digest[:12]}"
    )

    root = Path(output_root)
    destination = root / RAW_SUBDIRECTORY
    raw_path = destination / f"{stem}.json"
    metadata_path = destination / f"{stem}.metadata.json"
    relative_raw_path = raw_path.relative_to(root).as_posix()
    metadata = {
        "metadata_schema_version": METADATA_SCHEMA_VERSION,
        "retrieved_at_utc": timestamp.isoformat().replace("+00:00", "Z"),
        "provider": PROVIDER,
        "dataset": DATASET,
        "endpoint_path": FLOOD_FREQUENCY_PATH,
        "query_parameters": {
            "pv_idn": config.province_id,
            "limit": SAMPLE_LIMIT,
            "offset": SAMPLE_OFFSET,
        },
        "http_status": response.status_code,
        "content_type": response.content_type,
        "byte_count": len(raw_bytes),
        "sha256": digest,
        "relative_raw_artifact_path": relative_raw_path,
    }
    metadata_bytes = (
        json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    if raw_path.exists() or metadata_path.exists():
        if (
            raw_path.is_file()
            and metadata_path.is_file()
            and raw_path.read_bytes() == raw_bytes
            and metadata_path.read_bytes() == metadata_bytes
        ):
            return RawIngestionResult(raw_path, metadata_path, digest, created=False)
        raise FileExistsError("raw ingestion destination already exists")

    destination.mkdir(parents=True, exist_ok=True)
    _write_pair_atomically(raw_path, raw_bytes, metadata_path, metadata_bytes)
    return RawIngestionResult(raw_path, metadata_path, digest, created=True)


def _normalize_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _filesystem_safe(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    return safe or "unknown"


def _write_pair_atomically(
    raw_path: Path,
    raw_bytes: bytes,
    metadata_path: Path,
    metadata_bytes: bytes,
) -> None:
    """Publish each file atomically with best-effort pair rollback."""

    raw_temp: str | None = None
    metadata_temp: str | None = None
    raw_created = False
    try:
        raw_temp = _write_temp(raw_path.parent, raw_path.name, raw_bytes)
        metadata_temp = _write_temp(
            metadata_path.parent, metadata_path.name, metadata_bytes
        )
        _publish_no_replace(raw_temp, raw_path)
        raw_created = True
        _publish_no_replace(metadata_temp, metadata_path)
    except OSError:
        if raw_created and raw_path.exists():
            try:
                raw_path.unlink()
            except OSError:
                pass
        raise
    finally:
        if raw_temp is not None:
            Path(raw_temp).unlink(missing_ok=True)
        if metadata_temp is not None:
            Path(metadata_temp).unlink(missing_ok=True)


def _publish_no_replace(temporary_path: str, destination_path: Path) -> None:
    """Atomically create a destination without replacing an existing path."""

    os.link(temporary_path, destination_path)
    Path(temporary_path).unlink()


def _write_temp(directory: Path, target_name: str, content: bytes) -> str:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=directory,
        prefix=f".{target_name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        temporary.write(content)
        temporary.flush()
        os.fsync(temporary.fileno())
        return temporary.name
