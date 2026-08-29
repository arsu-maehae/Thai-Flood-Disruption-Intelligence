"""Credential-safe source ingestion for GISTDA flood recurrence data."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from src.configuration import GistdaConfig
from src.ingestion.gistda_client import FLOOD_FREQUENCY_PATH, GistdaClient


SAMPLE_LIMIT: Final = 10
SAMPLE_OFFSET: Final = 0
METADATA_SCHEMA_VERSION: Final = "1.0"
SANITIZATION_SCHEMA_VERSION: Final = "1.0"
PROVIDER: Final = "GISTDA"
DATASET: Final = "Historical Flood Recurrence"
SOURCE_SUBDIRECTORY: Final = Path("gistda/flood_freq/pattani")
# Project scope derived from previously observed behavior; this is not an
# officially documented mapping of province ID 94 to Pattani.
PATTANI_PROVINCE_ID: Final = "94"
_CREDENTIAL_NAMES: Final = frozenset({"api_key", "api-key"})


class SourceSanitizationError(ValueError):
    """Raised when a response cannot be safely prepared for persistence."""


@dataclass(frozen=True)
class SourceIngestionResult:
    """Paths and integrity data for one sanitized source artifact."""

    artifact_path: Path
    metadata_path: Path
    stored_artifact_sha256: str
    created: bool


@dataclass
class _SanitizationReport:
    field_names: set[str]
    query_parameter_names: set[str]
    removal_count: int = 0


def ingest_pattani_sample(
    *,
    config: GistdaConfig,
    client: GistdaClient,
    output_root: str | Path,
    retrieved_at: datetime | None = None,
) -> SourceIngestionResult:
    """Retrieve and persist one credential-sanitized Pattani sample response."""

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

    original_bytes = response.content
    original_digest = hashlib.sha256(original_bytes).hexdigest()
    original_byte_count = len(original_bytes)
    sanitized_payload, report = _sanitize_response(original_bytes)
    stored_bytes = _serialize_sanitized(sanitized_payload)
    _verify_sanitized(sanitized_payload, stored_bytes, config.api_key)
    stored_digest = hashlib.sha256(stored_bytes).hexdigest()

    timestamp_text = timestamp.strftime("%Y%m%dT%H%M%S%fZ")
    safe_province_id = _filesystem_safe(config.province_id)
    stem = (
        f"{timestamp_text}__pv_idn-{safe_province_id}"
        f"__limit-{SAMPLE_LIMIT}__offset-{SAMPLE_OFFSET}"
        f"__sha256-{stored_digest[:12]}"
    )

    root = Path(output_root)
    destination = root / SOURCE_SUBDIRECTORY
    artifact_path = destination / f"{stem}.sanitized.json"
    metadata_path = destination / f"{stem}.metadata.json"
    relative_artifact_path = artifact_path.relative_to(root).as_posix()
    metadata = {
        "metadata_schema_version": METADATA_SCHEMA_VERSION,
        "artifact_type": "sanitized_source_response",
        "sanitization_schema_version": SANITIZATION_SCHEMA_VERSION,
        "sanitization_applied": True,
        "removed_credential_field_names": sorted(report.field_names),
        "removed_credential_query_parameter_names": sorted(
            report.query_parameter_names
        ),
        "removed_credential_count": report.removal_count,
        "original_response_sha256": original_digest,
        "original_response_byte_count": original_byte_count,
        "stored_artifact_sha256": stored_digest,
        "stored_artifact_byte_count": len(stored_bytes),
        "relative_stored_artifact_path": relative_artifact_path,
        "retrieved_at_utc": timestamp.isoformat().replace("+00:00", "Z"),
        "provider": PROVIDER,
        "dataset": DATASET,
        "endpoint_path": FLOOD_FREQUENCY_PATH,
        "request_parameters": {
            "pv_idn": config.province_id,
            "limit": SAMPLE_LIMIT,
            "offset": SAMPLE_OFFSET,
        },
        "http_status": response.status_code,
        "content_type": response.content_type,
        "original_response_persisted": False,
        "original_response_disposition": (
            "The original response body was not persisted"
        ),
    }
    metadata_bytes = _serialize_json(metadata)
    _verify_no_configured_key(metadata_bytes, config.api_key)

    if artifact_path.exists() or metadata_path.exists():
        if (
            artifact_path.is_file()
            and metadata_path.is_file()
            and artifact_path.read_bytes() == stored_bytes
            and metadata_path.read_bytes() == metadata_bytes
        ):
            return SourceIngestionResult(
                artifact_path,
                metadata_path,
                stored_digest,
                created=False,
            )
        raise FileExistsError("source ingestion destination already exists")

    destination.mkdir(parents=True, exist_ok=True)
    _write_pair_atomically(
        artifact_path,
        stored_bytes,
        metadata_path,
        metadata_bytes,
    )
    return SourceIngestionResult(
        artifact_path,
        metadata_path,
        stored_digest,
        created=True,
    )


def _sanitize_response(original_bytes: bytes) -> tuple[Any, _SanitizationReport]:
    try:
        payload = json.loads(original_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SourceSanitizationError(
            "source response is not valid JSON and was not persisted"
        ) from None

    report = _SanitizationReport(set(), set())
    try:
        sanitized = _sanitize_value(payload, report)
    except (TypeError, ValueError):
        raise SourceSanitizationError(
            "source response sanitization failed and was not persisted"
        ) from None
    return sanitized, report


def _sanitize_value(value: Any, report: _SanitizationReport) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object key is not a string")
            if key.casefold() in _CREDENTIAL_NAMES:
                report.field_names.add(key)
                report.removal_count += 1
                continue
            if key.casefold() == "href" and isinstance(child, str):
                sanitized[key] = _sanitize_url(child, report)
            else:
                sanitized[key] = _sanitize_value(child, report)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(item, report) for item in value]
    return value


def _sanitize_url(url: str, report: _SanitizationReport) -> str:
    parts = urlsplit(url)
    safe_query: list[tuple[str, str]] = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        if name.casefold() == "api_key":
            report.query_parameter_names.add(name)
            report.removal_count += 1
        else:
            safe_query.append((name, value))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(safe_query), parts.fragment)
    )


def _serialize_sanitized(payload: Any) -> bytes:
    try:
        return _serialize_json(payload)
    except (TypeError, ValueError):
        raise SourceSanitizationError(
            "sanitized source response could not be serialized"
        ) from None


def _serialize_json(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _verify_sanitized(payload: Any, stored_bytes: bytes, api_key: str) -> None:
    try:
        _verify_value(payload, api_key)
        _verify_no_configured_key(stored_bytes, api_key)
    except (TypeError, ValueError):
        raise SourceSanitizationError(
            "sanitized source response failed credential-safety verification"
        ) from None


def _verify_value(value: Any, api_key: str) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in _CREDENTIAL_NAMES:
                raise ValueError("credential field remains")
            if key.casefold() == "href" and isinstance(child, str):
                if any(
                    name.casefold() == "api_key"
                    for name, _ in parse_qsl(
                        urlsplit(child).query,
                        keep_blank_values=True,
                    )
                ):
                    raise ValueError("credential query parameter remains")
            _verify_value(child, api_key)
    elif isinstance(value, list):
        for item in value:
            _verify_value(item, api_key)
    elif isinstance(value, str) and api_key:
        if api_key in value or api_key in unquote(value):
            raise ValueError("configured credential remains in string value")


def _verify_no_configured_key(content: bytes, api_key: str) -> None:
    if api_key and api_key.encode("utf-8") in content:
        raise ValueError("configured credential remains in persisted content")


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
    artifact_path: Path,
    artifact_bytes: bytes,
    metadata_path: Path,
    metadata_bytes: bytes,
) -> None:
    """Publish each file atomically with best-effort pair rollback."""

    artifact_temp: str | None = None
    metadata_temp: str | None = None
    artifact_created = False
    try:
        artifact_temp = _write_temp(
            artifact_path.parent,
            artifact_path.name,
            artifact_bytes,
        )
        metadata_temp = _write_temp(
            metadata_path.parent,
            metadata_path.name,
            metadata_bytes,
        )
        _publish_no_replace(artifact_temp, artifact_path)
        artifact_created = True
        _publish_no_replace(metadata_temp, metadata_path)
    except OSError:
        if artifact_created and artifact_path.exists():
            try:
                artifact_path.unlink()
            except OSError:
                pass
        raise
    finally:
        if artifact_temp is not None:
            Path(artifact_temp).unlink(missing_ok=True)
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
