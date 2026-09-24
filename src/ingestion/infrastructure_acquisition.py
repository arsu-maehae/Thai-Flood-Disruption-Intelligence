"""Bounded, immutable acquisition for reviewed Pattani infrastructure sources.

Metadata revalidation produces candidate evidence only.  Acquisition requires a
separately constructed, immutable :class:`ApprovedResourceSpec`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import Message
import hashlib
import html
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
from typing import Callable, Mapping, Protocol
from urllib.parse import unquote_plus, urlsplit
import zipfile

import requests


DRR_RESOURCE_ID = "4b670506-05e3-4cb2-80d7-06fc0745d21d"
DRR_CATALOG_URL = (
    "https://datagov.mot.go.th/th/dataset/dataset_41_01/resource/"
    f"{DRR_RESOURCE_ID}"
)
DGA_CSV_RESOURCE_ID = "2d45b0c6-75e9-4463-888d-ef364ad164fb"
DGA_ZIP_RESOURCE_ID = "c12a9e8d-8fa6-49a8-80d8-596e66329088"
DGA_CATALOG_URL = "https://data.go.th/th/dataset/health-citizeninfo"
DGA_CSV_DOWNLOAD_URL = (
    "https://data.go.th/dataset/00170665-bda1-4f4a-ad7c-52dac7abc7a5/"
    f"resource/{DGA_CSV_RESOURCE_ID}/download/citizeninfo_health_20200314.csv"
)
DGA_ZIP_DOWNLOAD_URL = (
    "https://data.go.th/dataset/00170665-bda1-4f4a-ad7c-52dac7abc7a5/"
    f"resource/{DGA_ZIP_RESOURCE_ID}/download/citizeninfo_health_20200314.zip"
)
GEOFABRIK_THAILAND_RESOURCE_ID = "thailand-260923.osm.pbf"
GEOFABRIK_THAILAND_CATALOG_URL = "https://download.geofabrik.de/asia/thailand.html"
GEOFABRIK_THAILAND_PBF_URL = (
    "https://download.geofabrik.de/asia/thailand-260923.osm.pbf"
)
GEOFABRIK_THAILAND_EXPECTED_BYTES = 327_676_785
GEOFABRIK_THAILAND_PROVIDER_MD5 = "4558c600b0e70e355c4436bd3ca80ac9"
GEOFABRIK_THAILAND_DOWNLOAD_CAP = 419_430_400
GEOFABRIK_PROVIDER_LABEL = "OpenStreetMap data distributed by Geofabrik"
GEOFABRIK_COVERAGE_LABEL = "Thailand"

METADATA_RESPONSE_LIMIT = 2 * 1024 * 1024
DEFAULT_METADATA_TIMEOUT = (10.0, 30.0)
DEFAULT_DOWNLOAD_TIMEOUT = (10.0, 120.0)
ACQUISITION_METADATA_SCHEMA_VERSION = "1.0"
_MEDIA_TYPE_TOKEN = re.compile(r"^[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+$")
_UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_APPROVAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CREDENTIAL_NAMES = frozenset({"api_key", "api-key", "authorization"})
_ZIP_NESTED_SUFFIXES = (
    ".zip", ".7z", ".rar", ".tar", ".tgz", ".gz", ".bz2", ".xz",
)
_MD5 = re.compile(r"^[0-9a-f]{32}$")
_PBF_HEADER_MAX_BYTES = 64 * 1024
_PBF_BLOB_MAX_BYTES = 32 * 1024 * 1024


class _Response(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def iter_content(self, chunk_size: int = ...) -> object: ...
    def close(self) -> None: ...


class _Session(Protocol):
    def get(self, url: str, **kwargs: object) -> _Response: ...


class AcquisitionError(RuntimeError):
    """Credential-safe error with explicit publication outcomes."""

    def __init__(
        self,
        category: str,
        *,
        request_count: int = 0,
        artifact_published: bool = False,
        metadata_published: bool = False,
        rollback_attempted: bool = False,
        rollback_succeeded: bool | None = None,
        cleanup_failed: bool = False,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.request_count = request_count
        self.artifact_published = artifact_published
        self.metadata_published = metadata_published
        self.rollback_attempted = rollback_attempted
        self.rollback_succeeded = rollback_succeeded
        self.cleanup_failed = cleanup_failed

    def __repr__(self) -> str:
        return (
            "AcquisitionError("
            f"category={self.category!r}, request_count={self.request_count}, "
            f"artifact_published={self.artifact_published}, "
            f"metadata_published={self.metadata_published}, "
            f"rollback_attempted={self.rollback_attempted}, "
            f"rollback_succeeded={self.rollback_succeeded}, "
            f"cleanup_failed={self.cleanup_failed})"
        )


@dataclass(frozen=True)
class ArchiveLimits:
    max_members: int
    max_member_uncompressed_bytes: int
    max_total_uncompressed_bytes: int
    max_compression_ratio: float

    def __post_init__(self) -> None:
        values = (
            self.max_members,
            self.max_member_uncompressed_bytes,
            self.max_total_uncompressed_bytes,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError("archive_limits_invalid")
        if (
            isinstance(self.max_compression_ratio, bool)
            or not isinstance(self.max_compression_ratio, (int, float))
            or not math.isfinite(self.max_compression_ratio)
            or self.max_compression_ratio <= 0
        ):
            raise ValueError("archive_limits_invalid")


@dataclass(frozen=True)
class CandidateResourceEvidence:
    source_key: str
    resource_id: str
    catalog_url: str
    candidate_download_url: str | None
    expected_format: str


@dataclass(frozen=True)
class MetadataRevalidationResult:
    candidates: tuple[CandidateResourceEvidence, ...]
    request_count: int
    authorizes_acquisition: bool = False


@dataclass(frozen=True)
class ApprovedResourceSpec:
    """An explicit operator-reviewed resource specification."""

    source_key: str
    resource_id: str
    catalog_url: str
    download_url: str
    expected_format: str
    approved_media_types: tuple[str, ...]
    max_bytes: int
    approval_reference: str
    archive_limits: ArchiveLimits | None = None
    expected_bytes: int | None = None
    expected_md5: str | None = None
    provider_label: str | None = None
    coverage_label: str | None = None

    @classmethod
    def approve(
        cls,
        evidence: CandidateResourceEvidence,
        *,
        approved_media_types: tuple[str, ...],
        max_bytes: int,
        approval_reference: str,
        archive_limits: ArchiveLimits | None = None,
    ) -> "ApprovedResourceSpec":
        if evidence.candidate_download_url is None:
            raise AcquisitionError("download_url_unresolved")
        return cls(
            source_key=evidence.source_key,
            resource_id=evidence.resource_id,
            catalog_url=evidence.catalog_url,
            download_url=evidence.candidate_download_url,
            expected_format=evidence.expected_format,
            approved_media_types=approved_media_types,
            max_bytes=max_bytes,
            approval_reference=approval_reference,
            archive_limits=archive_limits,
        )

    def __post_init__(self) -> None:
        if self.source_key not in {
            "drr_roads",
            "dga_healthcare",
            "geofabrik_osm_roads",
        }:
            raise ValueError("resource_spec_invalid")
        if self.source_key != "geofabrik_osm_roads" and not _UUID.fullmatch(
            self.resource_id
        ):
            raise ValueError("resource_spec_invalid")
        if self.expected_format not in {"csv", "zip", "osm.pbf"}:
            raise ValueError("resource_spec_invalid")
        if self.source_key == "drr_roads":
            try:
                download_path = urlsplit(self.download_url).path
            except ValueError:
                raise ValueError("resource_spec_invalid") from None
            if (
                self.expected_format != "zip"
                or self.resource_id != DRR_RESOURCE_ID
                or self.catalog_url != DRR_CATALOG_URL
                or f"/resource/{DRR_RESOURCE_ID}/download/" not in download_path
            ):
                raise ValueError("resource_spec_invalid")
        if self.source_key == "dga_healthcare" and (
            self.expected_format != "csv"
            or self.resource_id != DGA_CSV_RESOURCE_ID
            or self.catalog_url != DGA_CATALOG_URL
            or self.download_url != DGA_CSV_DOWNLOAD_URL
        ):
            raise ValueError("resource_spec_invalid")
        if self.source_key == "geofabrik_osm_roads" and (
            self.resource_id != GEOFABRIK_THAILAND_RESOURCE_ID
            or self.catalog_url != GEOFABRIK_THAILAND_CATALOG_URL
            or self.download_url != GEOFABRIK_THAILAND_PBF_URL
            or self.expected_format != "osm.pbf"
            or self.approved_media_types != ("application/octet-stream",)
            or self.max_bytes != GEOFABRIK_THAILAND_DOWNLOAD_CAP
            or self.expected_bytes != GEOFABRIK_THAILAND_EXPECTED_BYTES
            or self.expected_md5 != GEOFABRIK_THAILAND_PROVIDER_MD5
            or self.provider_label != GEOFABRIK_PROVIDER_LABEL
            or self.coverage_label != GEOFABRIK_COVERAGE_LABEL
        ):
            raise ValueError("resource_spec_invalid")
        _validate_official_url(self.catalog_url, self.source_key)
        _validate_official_url(self.download_url, self.source_key)
        if isinstance(self.max_bytes, bool) or not isinstance(self.max_bytes, int) or self.max_bytes <= 0:
            raise ValueError("resource_spec_invalid")
        if not _SAFE_APPROVAL.fullmatch(self.approval_reference):
            raise ValueError("resource_spec_invalid")
        if not self.approved_media_types:
            raise ValueError("resource_spec_invalid")
        normalized = tuple(_parse_media_type(value) for value in self.approved_media_types)
        if normalized != self.approved_media_types or len(set(normalized)) != len(normalized):
            raise ValueError("resource_spec_invalid")
        format_media_types = {
            "csv": {"text/csv", "application/csv"},
            "zip": {"application/zip", "application/x-zip-compressed"},
            "osm.pbf": {"application/octet-stream"},
        }
        if not set(normalized).issubset(format_media_types[self.expected_format]):
            raise ValueError("resource_spec_invalid")
        if self.expected_format == "zip" and self.archive_limits is None:
            raise ValueError("resource_spec_invalid")
        if self.expected_format != "zip" and self.archive_limits is not None:
            raise ValueError("resource_spec_invalid")
        if self.expected_bytes is not None and (
            isinstance(self.expected_bytes, bool)
            or not isinstance(self.expected_bytes, int)
            or self.expected_bytes <= 0
            or self.expected_bytes > self.max_bytes
        ):
            raise ValueError("resource_spec_invalid")
        if self.expected_md5 is not None and (
            not isinstance(self.expected_md5, str)
            or not _MD5.fullmatch(self.expected_md5)
        ):
            raise ValueError("resource_spec_invalid")
        if self.source_key != "geofabrik_osm_roads" and any(
            value is not None
            for value in (
                self.expected_bytes,
                self.expected_md5,
                self.provider_label,
                self.coverage_label,
            )
        ):
            raise ValueError("resource_spec_invalid")


@dataclass(frozen=True)
class AcquisitionResult:
    artifact_path: Path
    metadata_path: Path
    sha256: str
    byte_count: int
    content_type: str
    request_count: int
    reused: bool


@dataclass(frozen=True)
class _MetadataTarget:
    source_key: str
    resource_id: str
    catalog_url: str
    expected_format: str
    known_download_url: str | None


_METADATA_TARGETS = (
    _MetadataTarget("drr_roads", DRR_RESOURCE_ID, DRR_CATALOG_URL, "zip", None),
    _MetadataTarget(
        "dga_healthcare",
        DGA_CSV_RESOURCE_ID,
        DGA_CATALOG_URL,
        "csv",
        DGA_CSV_DOWNLOAD_URL,
    ),
)


def revalidate_selected_metadata(
    session: _Session,
    *,
    timeout: tuple[float, float] = DEFAULT_METADATA_TIMEOUT,
    response_limit: int = METADATA_RESPONSE_LIMIT,
) -> MetadataRevalidationResult:
    """Fetch two official catalog pages and return non-authorizing evidence."""

    _validate_timeout(timeout)
    _validate_positive_int(response_limit, "metadata_limit_invalid")
    candidates: list[CandidateResourceEvidence] = []
    request_count = 0
    for target in _METADATA_TARGETS:
        _require_retry_disabled(session, target.catalog_url)
        request_count += 1
        try:
            response = session.get(
                target.catalog_url,
                stream=True,
                allow_redirects=False,
                timeout=timeout,
            )
        except requests.Timeout:
            raise AcquisitionError("metadata_timeout", request_count=request_count) from None
        except requests.ConnectionError:
            raise AcquisitionError("metadata_connection_failed", request_count=request_count) from None
        except requests.RequestException:
            raise AcquisitionError("metadata_request_failed", request_count=request_count) from None
        try:
            if response.status_code != 200:
                category = "metadata_redirect_rejected" if 300 <= response.status_code < 400 else "metadata_http_error"
                raise AcquisitionError(category, request_count=request_count)
            media_type = _response_media_type(response, request_count=request_count)
            if media_type != "text/html":
                raise AcquisitionError("metadata_content_type_rejected", request_count=request_count)
            content = _read_limited(response, response_limit, "metadata_response_too_large", request_count)
        finally:
            try:
                response.close()
            except Exception:
                raise AcquisitionError(
                    "metadata_response_close_failed", request_count=request_count
                ) from None
        try:
            text = content.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise AcquisitionError("metadata_encoding_rejected", request_count=request_count) from None
        try:
            candidate_url = _find_resource_download_url(text, target)
        except AcquisitionError as error:
            raise AcquisitionError(error.category, request_count=request_count) from None
        if target.known_download_url is not None and candidate_url != target.known_download_url:
            raise AcquisitionError("metadata_resource_mismatch", request_count=request_count)
        candidates.append(
            CandidateResourceEvidence(
                source_key=target.source_key,
                resource_id=target.resource_id,
                catalog_url=target.catalog_url,
                candidate_download_url=candidate_url,
                expected_format=target.expected_format,
            )
        )
    return MetadataRevalidationResult(tuple(candidates), request_count)


def acquire_resource(
    spec: ApprovedResourceSpec,
    session: _Session,
    output_root: str | Path,
    *,
    minimum_free_bytes: int,
    timeout: tuple[float, float] = DEFAULT_DOWNLOAD_TIMEOUT,
    clock: Callable[[], datetime] | None = None,
) -> AcquisitionResult:
    """Download, validate, and immutably publish one explicitly approved resource."""

    if not isinstance(spec, ApprovedResourceSpec):
        raise AcquisitionError("approved_resource_required")
    _validate_timeout(timeout)
    _validate_positive_int(minimum_free_bytes, "free_space_floor_invalid", allow_zero=True)
    timestamp = _normalize_timestamp((clock or _utc_now)())
    try:
        root = Path(output_root).resolve()
        destination = root / _destination_parts(spec.source_key)
    except (OSError, RuntimeError):
        raise AcquisitionError("output_path_invalid") from None
    _validate_destination_containment(root, destination)
    _require_retry_disabled(session, spec.download_url)
    try:
        free_bytes = _free_bytes_for(root)
    except OSError:
        raise AcquisitionError("free_space_check_failed") from None
    if free_bytes < minimum_free_bytes:
        raise AcquisitionError("insufficient_free_space")
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise AcquisitionError("directory_creation_failed") from None
    _validate_destination_containment(root, destination)

    temporary_path: Path | None = None
    response: _Response | None = None
    request_count = 1
    try:
        try:
            response = session.get(
                spec.download_url,
                stream=True,
                allow_redirects=False,
                timeout=timeout,
            )
        except requests.Timeout:
            raise AcquisitionError("download_timeout", request_count=1) from None
        except requests.ConnectionError:
            raise AcquisitionError("download_connection_failed", request_count=1) from None
        except requests.RequestException:
            raise AcquisitionError("download_request_failed", request_count=1) from None
        if response.status_code != 200:
            category = "download_redirect_rejected" if 300 <= response.status_code < 400 else "download_http_error"
            raise AcquisitionError(category, request_count=1)
        media_type = _response_media_type(response, request_count=1)
        if media_type not in spec.approved_media_types:
            raise AcquisitionError("download_content_type_rejected", request_count=1)
        declared_length = _declared_length(response.headers)
        if declared_length is not None and declared_length > spec.max_bytes:
            raise AcquisitionError("download_too_large", request_count=1)
        if spec.expected_bytes is not None and declared_length is not None:
            if declared_length != spec.expected_bytes:
                raise AcquisitionError("content_length_mismatch", request_count=1)
        temporary_path, digest, provider_md5, byte_count = _stream_to_temporary(
            response, destination, spec.max_bytes
        )
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                cleanup_failed = not _cleanup_paths(temporary_path)
                temporary_path = None
                raise AcquisitionError(
                    "download_response_close_failed",
                    request_count=1,
                    cleanup_failed=cleanup_failed,
                ) from None

    try:
        if spec.expected_bytes is not None and byte_count != spec.expected_bytes:
            raise AcquisitionError("download_length_mismatch", request_count=1)
        if spec.expected_md5 is not None and provider_md5 != spec.expected_md5:
            raise AcquisitionError("provider_checksum_mismatch", request_count=1)
        archive_summary = _inspection_summary(temporary_path, spec)
        extension = {
            "zip": ".zip",
            "csv": ".csv",
            "osm.pbf": ".osm.pbf",
        }[spec.expected_format]
        stem = f"resource-{spec.resource_id}__sha256-{digest}"
        artifact_path = destination / f"{stem}{extension}"
        metadata_path = destination / f"{stem}.metadata.json"
        relative_artifact = artifact_path.relative_to(root).as_posix()
        metadata: dict[str, object] = {
            "schema_version": ACQUISITION_METADATA_SCHEMA_VERSION,
            "provider": spec.provider_label or spec.source_key,
            "resource_id": spec.resource_id,
            "catalog_url": spec.catalog_url,
            "approved_download_url": spec.download_url,
            "approval_reference": spec.approval_reference,
            "retrieved_at_utc": timestamp.isoformat().replace("+00:00", "Z"),
            "http_status": 200,
            "content_type": media_type,
            "request_count": request_count,
            "redirect_count": 0,
            "byte_count": byte_count,
            "sha256": digest,
            "relative_artifact_path": relative_artifact,
            "max_bytes": spec.max_bytes,
            "archive_inspection": archive_summary,
        }
        if spec.source_key == "geofabrik_osm_roads":
            metadata.update(
                {
                    "source_key": spec.source_key,
                    "coverage": spec.coverage_label,
                    "provider_md5": provider_md5,
                    "provider_md5_role": (
                        "provider integrity evidence; not cryptographic authenticity"
                    ),
                    "expected_byte_count": spec.expected_bytes,
                }
            )
        metadata_bytes = _json_bytes(metadata)
        reuse = _verify_reusable_pair(
            artifact_path, metadata_path, metadata, root
        )
        if reuse is True:
            _cleanup_temporary_or_raise(temporary_path, request_count=request_count)
            return AcquisitionResult(
                artifact_path, metadata_path, digest, byte_count, media_type, 1, True
            )
        if reuse is False:
            raise AcquisitionError("destination_collision", request_count=1)
        _publish_pair(
            temporary_path,
            artifact_path,
            metadata_path,
            metadata_bytes,
            request_count=request_count,
        )
        temporary_path = None
        return AcquisitionResult(
            artifact_path, metadata_path, digest, byte_count, media_type, 1, False
        )
    except AcquisitionError as error:
        cleanup_failed = not _cleanup_paths(temporary_path)
        temporary_path = None
        if cleanup_failed and not error.cleanup_failed:
            raise AcquisitionError(
                error.category,
                request_count=error.request_count,
                artifact_published=error.artifact_published,
                metadata_published=error.metadata_published,
                rollback_attempted=error.rollback_attempted,
                rollback_succeeded=error.rollback_succeeded,
                cleanup_failed=True,
            ) from None
        raise
    except (OSError, ValueError, zipfile.BadZipFile):
        cleanup_failed = not _cleanup_paths(temporary_path)
        temporary_path = None
        raise AcquisitionError(
            "resource_validation_failed",
            request_count=1,
            cleanup_failed=cleanup_failed,
        ) from None


def _find_resource_download_url(text: str, target: _MetadataTarget) -> str | None:
    decoded = html.unescape(text).replace("\\/", "/")
    pattern = re.compile(r"https://[^\s\"'<>]+", re.IGNORECASE)
    candidates: set[str] = set()
    marker = f"/resource/{target.resource_id}/download/"
    for match in pattern.findall(decoded):
        value = match.rstrip("),.;")
        if marker not in value:
            continue
        try:
            _validate_official_url(value, target.source_key)
        except ValueError:
            continue
        candidates.add(value)
    if len(candidates) > 1:
        raise AcquisitionError("metadata_resource_ambiguous")
    return next(iter(candidates), None)


def _validate_official_url(value: str, source_key: str) -> None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ValueError("resource_spec_invalid") from None
    expected_hosts = {
        "drr_roads": "datagov.mot.go.th",
        "dga_healthcare": "data.go.th",
        "geofabrik_osm_roads": "download.geofabrik.de",
    }
    expected_host = expected_hosts.get(source_key)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("resource_spec_invalid")


def _parse_media_type(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("content_type_invalid")
    message = Message()
    message["content-type"] = value
    media_type = message.get_content_type().lower()
    raw_type = value.split(";", 1)[0].strip().lower()
    if media_type != raw_type or not _MEDIA_TYPE_TOKEN.fullmatch(media_type):
        raise ValueError("content_type_invalid")
    return media_type


GEOFABRIK_THAILAND_PBF_SPEC = ApprovedResourceSpec(
    source_key="geofabrik_osm_roads",
    resource_id=GEOFABRIK_THAILAND_RESOURCE_ID,
    catalog_url=GEOFABRIK_THAILAND_CATALOG_URL,
    download_url=GEOFABRIK_THAILAND_PBF_URL,
    expected_format="osm.pbf",
    approved_media_types=("application/octet-stream",),
    max_bytes=GEOFABRIK_THAILAND_DOWNLOAD_CAP,
    approval_reference="phase3b-osm-contract-20260924",
    expected_bytes=GEOFABRIK_THAILAND_EXPECTED_BYTES,
    expected_md5=GEOFABRIK_THAILAND_PROVIDER_MD5,
    provider_label=GEOFABRIK_PROVIDER_LABEL,
    coverage_label=GEOFABRIK_COVERAGE_LABEL,
)


def _response_media_type(response: _Response, *, request_count: int) -> str:
    value = response.headers.get("Content-Type") or response.headers.get("content-type")
    try:
        return _parse_media_type(value)
    except ValueError:
        raise AcquisitionError("content_type_invalid", request_count=request_count) from None


def _read_limited(
    response: _Response, limit: int, category: str, request_count: int
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        iterator = response.iter_content(chunk_size=64 * 1024)
        for chunk in iterator:  # type: ignore[union-attr]
            if not isinstance(chunk, bytes):
                raise AcquisitionError("response_stream_invalid", request_count=request_count)
            if not chunk:
                continue
            total += len(chunk)
            if total > limit:
                raise AcquisitionError(category, request_count=request_count)
            chunks.append(chunk)
    except AcquisitionError:
        raise
    except requests.Timeout:
        raise AcquisitionError("response_stream_timeout", request_count=request_count) from None
    except (OSError, requests.ConnectionError):
        raise AcquisitionError("response_stream_failed", request_count=request_count) from None
    except requests.RequestException:
        raise AcquisitionError("response_stream_failed", request_count=request_count) from None
    return b"".join(chunks)


def _stream_to_temporary(
    response: _Response, destination: Path, limit: int
) -> tuple[Path, str, str, int]:
    temporary: tempfile._TemporaryFileWrapper[bytes] | None = None
    path: Path | None = None
    digest = hashlib.sha256()
    provider_digest = hashlib.md5(usedforsecurity=False)
    total = 0
    try:
        temporary = tempfile.NamedTemporaryFile(
            mode="wb", dir=destination, prefix=".download.", suffix=".tmp", delete=False
        )
        path = Path(temporary.name)
        try:
            iterator = response.iter_content(chunk_size=1024 * 1024)
            for chunk in iterator:  # type: ignore[union-attr]
                if not isinstance(chunk, bytes):
                    raise AcquisitionError("response_stream_invalid", request_count=1)
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise AcquisitionError("download_too_large", request_count=1)
                temporary.write(chunk)
                digest.update(chunk)
                provider_digest.update(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
        finally:
            temporary.close()
    except AcquisitionError as error:
        cleanup_failed = not _cleanup_paths(path)
        if cleanup_failed:
            raise _with_cleanup_failure(error) from None
        raise
    except requests.Timeout:
        cleanup_failed = not _cleanup_paths(path)
        raise AcquisitionError(
            "response_stream_timeout", request_count=1, cleanup_failed=cleanup_failed
        ) from None
    except Exception:
        cleanup_failed = not _cleanup_paths(path)
        raise AcquisitionError(
            "temporary_write_failed", request_count=1, cleanup_failed=cleanup_failed
        ) from None
    if path is None:
        raise AcquisitionError("temporary_write_failed", request_count=1)
    return path, digest.hexdigest(), provider_digest.hexdigest(), total


def _inspection_summary(
    path: Path, spec: ApprovedResourceSpec
) -> dict[str, object]:
    if spec.expected_format == "zip":
        return _inspect_zip_safely(path, spec.archive_limits)
    if spec.expected_format == "osm.pbf":
        return _inspect_osm_pbf_header(path)
    return {"applied": False}


def _inspect_osm_pbf_header(path: Path) -> dict[str, object]:
    """Validate only the bounded first BlobHeader and declared blob length."""

    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            raw_length = handle.read(4)
            if len(raw_length) != 4:
                raise AcquisitionError("pbf_header_rejected", request_count=1)
            header_length = int.from_bytes(raw_length, "big")
            if not 0 < header_length <= _PBF_HEADER_MAX_BYTES:
                raise AcquisitionError("pbf_header_rejected", request_count=1)
            header = handle.read(header_length)
            if len(header) != header_length:
                raise AcquisitionError("pbf_header_rejected", request_count=1)
        blob_type, blob_size = _parse_pbf_blob_header(header)
        if (
            blob_type != b"OSMHeader"
            or blob_size is None
            or not 0 < blob_size <= _PBF_BLOB_MAX_BYTES
            or size < 4 + header_length + blob_size
        ):
            raise AcquisitionError("pbf_header_rejected", request_count=1)
    except AcquisitionError:
        raise
    except (OSError, ValueError):
        raise AcquisitionError("pbf_header_rejected", request_count=1) from None
    return {
        "applied": True,
        "format": "osm.pbf",
        "first_blob_type": "OSMHeader",
        "blob_header_bytes": header_length,
        "declared_first_blob_bytes": blob_size,
        "max_blob_header_bytes": _PBF_HEADER_MAX_BYTES,
        "max_declared_first_blob_bytes": _PBF_BLOB_MAX_BYTES,
        "semantic_parsing_applied": False,
        "extracted": False,
    }


def _parse_pbf_blob_header(data: bytes) -> tuple[bytes | None, int | None]:
    position = 0
    blob_type: bytes | None = None
    blob_size: int | None = None
    while position < len(data):
        tag, position = _read_pbf_varint(data, position)
        field = tag >> 3
        wire = tag & 7
        if wire == 0:
            value, position = _read_pbf_varint(data, position)
            if field == 3:
                blob_size = value
        elif wire == 2:
            length, position = _read_pbf_varint(data, position)
            end = position + length
            if end > len(data):
                raise ValueError("truncated")
            if field == 1:
                blob_type = data[position:end]
            position = end
        elif wire == 1:
            position += 8
        elif wire == 5:
            position += 4
        else:
            raise ValueError("wire")
        if position > len(data):
            raise ValueError("truncated")
    return blob_type, blob_size


def _read_pbf_varint(data: bytes, position: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if position >= len(data):
            raise ValueError("truncated")
        byte = data[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
    raise ValueError("varint")


def _inspect_zip_safely(path: Path, limits: ArchiveLimits | None) -> dict[str, object]:
    if limits is None or not zipfile.is_zipfile(path):
        raise AcquisitionError("archive_rejected", request_count=1)
    member_count = 0
    total_uncompressed = 0
    normalized_names: set[str] = set()
    component_bases: dict[str, set[str]] = {suffix: set() for suffix in (".shp", ".shx", ".dbf")}
    prj_present = False
    with zipfile.ZipFile(path, mode="r") as archive:
        for info in archive.infolist():
            member_count += 1
            if member_count > limits.max_members:
                raise AcquisitionError("archive_limits_exceeded", request_count=1)
            name = info.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            if (
                not name
                or "\x00" in name
                or pure.is_absolute()
                or any(part in {"", ".", ".."} for part in pure.parts)
                or re.match(r"^[A-Za-z]:", name)
            ):
                raise AcquisitionError("archive_path_rejected", request_count=1)
            normalized = pure.as_posix().casefold()
            if normalized in normalized_names:
                raise AcquisitionError("archive_duplicate_member", request_count=1)
            normalized_names.add(normalized)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise AcquisitionError("archive_symlink_rejected", request_count=1)
            if info.is_dir():
                continue
            if mode not in (0, stat.S_IFREG):
                raise AcquisitionError("archive_member_type_rejected", request_count=1)
            if info.flag_bits & 0x1:
                raise AcquisitionError("archive_encryption_rejected", request_count=1)
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise AcquisitionError("archive_compression_rejected", request_count=1)
            if normalized.endswith(_ZIP_NESTED_SUFFIXES):
                raise AcquisitionError("nested_archive_rejected", request_count=1)
            if info.file_size > limits.max_member_uncompressed_bytes:
                raise AcquisitionError("archive_limits_exceeded", request_count=1)
            total_uncompressed += info.file_size
            if total_uncompressed > limits.max_total_uncompressed_bytes:
                raise AcquisitionError("archive_limits_exceeded", request_count=1)
            ratio = info.file_size / max(info.compress_size, 1)
            if ratio > limits.max_compression_ratio:
                raise AcquisitionError("archive_limits_exceeded", request_count=1)
            suffix = pure.suffix.casefold()
            if suffix in component_bases:
                component_bases[suffix].add(pure.with_suffix("").as_posix().casefold())
            elif suffix == ".prj":
                prj_present = True
    common_bases = set.intersection(*(component_bases[suffix] for suffix in component_bases))
    if not common_bases:
        raise AcquisitionError("shapefile_components_missing", request_count=1)
    return {
        "applied": True,
        "member_count": member_count,
        "total_uncompressed_bytes": total_uncompressed,
        "required_shapefile_components_present": True,
        "prj_present": prj_present,
        "crs_interpreted": False,
        "extracted": False,
    }


def _verify_reusable_pair(
    artifact: Path, metadata: Path, expected: dict[str, object], root: Path
) -> bool | None:
    try:
        artifact_exists = artifact.exists() or artifact.is_symlink()
        metadata_exists = metadata.exists() or metadata.is_symlink()
    except OSError:
        raise AcquisitionError("reuse_path_inspection_failed", request_count=1) from None
    if not artifact_exists and not metadata_exists:
        return None
    if not artifact_exists or not metadata_exists:
        return False
    try:
        artifact_stat = artifact.lstat()
        metadata_stat = metadata.lstat()
        if _is_link_or_reparse(artifact_stat) or _is_link_or_reparse(metadata_stat):
            raise AcquisitionError("reuse_path_rejected", request_count=1)
        if not stat.S_ISREG(artifact_stat.st_mode) or not stat.S_ISREG(metadata_stat.st_mode):
            raise AcquisitionError("reuse_path_rejected", request_count=1)
        resolved_artifact = artifact.resolve(strict=True)
        resolved_metadata = metadata.resolve(strict=True)
        resolved_artifact.relative_to(root)
        resolved_metadata.relative_to(root)
        artifact_bytes = artifact.read_bytes()
        stored_bytes = metadata.read_bytes()
        stored = json.loads(stored_bytes, object_pairs_hook=_reject_duplicate_pairs)
    except AcquisitionError:
        raise
    except ValueError:
        raise AcquisitionError("reuse_path_rejected", request_count=1) from None
    except OSError:
        raise AcquisitionError("reuse_path_inspection_failed", request_count=1) from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(stored, dict) or set(stored) != set(expected):
        return False
    if _json_bytes(stored) != stored_bytes:
        return False
    for key, value in expected.items():
        if key == "retrieved_at_utc":
            if not isinstance(stored.get(key), str) or not stored[key].endswith("Z"):
                return False
        elif stored.get(key) != value:
            return False
    if expected["relative_artifact_path"] != artifact.relative_to(root).as_posix():
        return False
    return (
        len(artifact_bytes) == expected["byte_count"]
        and hashlib.sha256(artifact_bytes).hexdigest() == expected["sha256"]
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _publish_pair(
    artifact_temp: Path,
    artifact: Path,
    metadata: Path,
    metadata_bytes: bytes,
    *,
    request_count: int,
) -> None:
    metadata_temp: Path | None = None
    artifact_published = False
    metadata_published = False
    rollback_attempted = False
    rollback_succeeded: bool | None = None
    cleanup_failed = False
    try:
        metadata_temp = _write_temporary(metadata.parent, ".metadata.", metadata_bytes)
        os.link(artifact_temp, artifact)
        artifact_published = True
        os.link(metadata_temp, metadata)
        metadata_published = True
    except AcquisitionError as error:
        artifact_cleanup_failed = not _cleanup_paths(artifact_temp)
        cleanup_failed = error.cleanup_failed or artifact_cleanup_failed
        raise AcquisitionError(
            "publication_failed",
            request_count=request_count,
            cleanup_failed=cleanup_failed,
        ) from None
    except OSError:
        if artifact_published and not metadata_published:
            rollback_attempted = True
            try:
                artifact.unlink()
                rollback_succeeded = True
            except OSError:
                rollback_succeeded = False
        cleanup_failed = not _cleanup_paths(artifact_temp, metadata_temp)
        raise AcquisitionError(
            "publication_failed",
            request_count=request_count,
            artifact_published=artifact_published,
            metadata_published=metadata_published,
            rollback_attempted=rollback_attempted,
            rollback_succeeded=rollback_succeeded,
            cleanup_failed=cleanup_failed,
        ) from None
    cleanup_failed = not _cleanup_paths(artifact_temp, metadata_temp)
    if cleanup_failed:
        raise AcquisitionError(
            "cleanup_failed",
            request_count=request_count,
            artifact_published=True,
            metadata_published=True,
            cleanup_failed=True,
        )


def _write_temporary(directory: Path, prefix: str, content: bytes) -> Path:
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=directory, prefix=prefix, suffix=".tmp", delete=False
        ) as handle:
            path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except OSError:
        cleanup_failed = not _cleanup_paths(path)
        raise AcquisitionError(
            "temporary_write_failed", cleanup_failed=cleanup_failed
        ) from None


def _cleanup_paths(*paths: Path | None) -> bool:
    success = True
    for path in paths:
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            success = False
    return success


def _cleanup_temporary_or_raise(path: Path, *, request_count: int) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        raise AcquisitionError(
            "cleanup_failed", request_count=request_count, cleanup_failed=True
        ) from None


def _with_cleanup_failure(error: AcquisitionError) -> AcquisitionError:
    return AcquisitionError(
        error.category,
        request_count=error.request_count,
        artifact_published=error.artifact_published,
        metadata_published=error.metadata_published,
        rollback_attempted=error.rollback_attempted,
        rollback_succeeded=error.rollback_succeeded,
        cleanup_failed=True,
    )


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _declared_length(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("Content-Length") or headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise AcquisitionError("content_length_invalid", request_count=1) from None
    if value < 0:
        raise AcquisitionError("content_length_invalid", request_count=1)
    return value


def _destination_parts(source_key: str) -> Path:
    destinations = {
        "drr_roads": Path("infrastructure/roads/drr"),
        "dga_healthcare": Path("infrastructure/healthcare/dga"),
        "geofabrik_osm_roads": Path("infrastructure/roads/osm/geofabrik"),
    }
    try:
        return destinations[source_key]
    except KeyError:
        raise AcquisitionError("approved_resource_required") from None


def _validate_destination_containment(root: Path, destination: Path) -> None:
    try:
        resolved_destination = destination.resolve(strict=False)
        resolved_destination.relative_to(root)
    except ValueError:
        raise AcquisitionError("destination_escape_rejected") from None
    except (OSError, RuntimeError):
        raise AcquisitionError("destination_inspection_failed") from None

    current = root
    try:
        relative = destination.relative_to(root)
        for part in relative.parts:
            current = current / part
            try:
                details = current.lstat()
            except FileNotFoundError:
                break
            if _is_link_or_reparse(details):
                raise AcquisitionError("destination_reparse_rejected")
    except AcquisitionError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise AcquisitionError("destination_inspection_failed") from None


def _is_link_or_reparse(details: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(details, "st_file_attributes", 0)
    return stat.S_ISLNK(details.st_mode) or bool(attributes & reparse_flag)


def _free_bytes_for(root: Path) -> int:
    probe = root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return os.statvfs(probe).f_bavail * os.statvfs(probe).f_frsize
    except AttributeError:
        import shutil

        return shutil.disk_usage(probe).free


def _validate_positive_int(value: object, category: str, *, allow_zero: bool = False) -> None:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AcquisitionError(category)


def _validate_timeout(value: object) -> None:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or any(
            isinstance(part, bool)
            or not isinstance(part, (int, float))
            or not math.isfinite(part)
            or part <= 0
            for part in value
        )
    ):
        raise AcquisitionError("timeout_invalid")


def _normalize_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AcquisitionError("timestamp_invalid")
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_retry_disabled(session: _Session, url: str) -> None:
    get_adapter = getattr(session, "get_adapter", None)
    if not callable(get_adapter):
        return
    try:
        retries = get_adapter(url).max_retries
        total = retries.total
    except (AttributeError, KeyError, ValueError):
        raise AcquisitionError("retry_policy_unverified") from None
    if total not in (0, False):
        raise AcquisitionError("retry_policy_rejected")
