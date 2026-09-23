"""Neutral deterministic transformation of validated Pattani source pages.

The transformation schema is a versioned project schema based on the observed
Phase 1 snapshot. It is not an official GISTDA response guarantee. Geometry is
preserved as JSON without coordinate interpretation, reprojection, or SRID.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from src.validation.flood_frequency import (
    PageValidationResult,
    ParsedSourcePage,
    SourceValidationError,
    _validated_features,
    validate_page_sequence,
)


TRANSFORMATION_SCHEMA_VERSION = "1.0"
TRANSFORMATION_POLICY_VERSION = "pattani-observed-neutral-v1"

_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_FEATURE_MEMBERS = frozenset({"type", "id", "properties", "geometry"})
_STRING_PROPERTIES = frozenset({
    "_collectionId",
    "_createdAt",
    "_createdBy",
    "_id",
    "_updatedAt",
    "_updatedBy",
    "ap_code",
    "ap_en",
    "ap_tn",
    "com_tn",
    "pv_code",
    "pv_en",
    "pv_tn",
    "re_nesdb",
    "re_royin",
    "tb_code",
    "tb_en",
    "tb_tn",
})
_INTEGER_PROPERTIES = frozenset({
    "ap_idn",
    "freq",
    "objectid",
    "pv_idn",
    "tb_idn",
    *(f"y_{year}" for year in range(2011, 2025)),
})
_NUMBER_PROPERTIES = frozenset({"area_rai", "shape_area", "shape_length"})
_PROPERTY_MEMBERS = _STRING_PROPERTIES | _INTEGER_PROPERTIES | _NUMBER_PROPERTIES
_SAFE_ERROR_CATEGORIES = frozenset({
    "cleanup_failed",
    "credential_verification_incomplete",
    "invalid_lineage",
    "invalid_transformation_input",
    "invalid_transformation_schema",
    "output_already_exists",
    "publication_failed",
    "serialization_failed",
    "source_validation_failed",
})


class TransformationError(ValueError):
    """Fixed-category error that never retains source data or identifiers."""

    def __init__(
        self,
        category: str,
        *,
        record_published: bool = False,
        completion_published: bool = False,
    ) -> None:
        safe = (
            category
            if category in _SAFE_ERROR_CATEGORIES
            else "invalid_transformation_input"
        )
        self.category = safe
        self.record_published = record_published is True
        self.completion_published = completion_published is True
        super().__init__(safe)

    def __repr__(self) -> str:
        return (
            "TransformationError("
            f"category={self.category!r}, "
            f"record_published={self.record_published!r}, "
            f"completion_published={self.completion_published!r})"
        )


@dataclass(frozen=True, repr=False)
class SourcePageLineage:
    """Safe page-level lineage excluded from object representations."""

    run_id: str
    page_index: int
    requested_offset: int
    requested_limit: int
    source_artifact_path: str
    source_metadata_path: str
    source_stored_sha256: str

    def __repr__(self) -> str:
        return "SourcePageLineage()"


@dataclass(frozen=True, repr=False)
class ValidatedTransformationPage:
    """A sealed Phase 2B page pair plus its journal-derived lineage."""

    parsed: ParsedSourcePage = field(repr=False)
    validation: PageValidationResult = field(repr=False)
    lineage: SourcePageLineage = field(repr=False)

    def __repr__(self) -> str:
        return "ValidatedTransformationPage()"


@dataclass(frozen=True, repr=False)
class TransformedPage:
    """One schema-checked page serialized in deterministic JSONL."""

    lineage: SourcePageLineage = field(repr=False)
    content: bytes = field(repr=False)
    record_count: int
    byte_count: int
    sha256: str

    def __repr__(self) -> str:
        return (
            "TransformedPage("
            f"record_count={self.record_count}, byte_count={self.byte_count}, "
            f"sha256={self.sha256!r})"
        )


@dataclass(frozen=True)
class TransformationResult:
    """Safe completion summary for an immutable transformed run."""

    run_id: str
    transformation_id: str
    directory: Path
    manifest_path: Path
    page_count: int
    feature_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "transformation_id": self.transformation_id,
            "directory": self.directory.as_posix(),
            "manifest_path": self.manifest_path.as_posix(),
            "page_count": self.page_count,
            "feature_count": self.feature_count,
        }


def transform_validated_page(
    parsed: ParsedSourcePage,
    validation: PageValidationResult,
    lineage: SourcePageLineage,
) -> TransformedPage:
    """Validate the observed project schema and serialize exactly one page."""

    try:
        features = _validated_features(parsed, validation)
    except SourceValidationError:
        raise TransformationError("source_validation_failed") from None
    _validate_lineage(lineage, validation)
    if validation.configured_key_check_complete is not True:
        raise TransformationError("credential_verification_incomplete")

    lines: list[bytes] = []
    for sequence, feature in enumerate(features):
        _validate_feature_schema(feature)
        try:
            line = json.dumps(
                {"feature_sequence": sequence, "feature": feature},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
            raise TransformationError("serialization_failed") from None
        lines.append(line + b"\n")

    content = b"".join(lines)
    digest = hashlib.sha256(content).hexdigest()
    return TransformedPage(
        lineage=lineage,
        content=content,
        record_count=len(features),
        byte_count=len(content),
        sha256=digest,
    )


def publish_transformed_run(
    pages: Iterable[ValidatedTransformationPage],
    *,
    output_root: Path,
    run_id: str,
    transformation_id: str,
) -> TransformationResult:
    """Publish page files without replacement and publish the manifest last."""

    _validate_identifier(run_id)
    _validate_identifier(transformation_id)
    try:
        sequence = tuple(pages)
    except Exception:
        raise TransformationError("invalid_transformation_input") from None
    if not sequence or not all(
        isinstance(page, ValidatedTransformationPage) for page in sequence
    ):
        raise TransformationError("invalid_transformation_input")

    validations = tuple(page.validation for page in sequence)
    try:
        run_validation = validate_page_sequence(validations)
    except SourceValidationError:
        raise TransformationError("source_validation_failed") from None
    if run_validation.configured_key_check_complete is not True:
        raise TransformationError("credential_verification_incomplete")
    for page in sequence:
        _validate_lineage(page.lineage, page.validation)
        if page.lineage.run_id != run_id:
            raise TransformationError("invalid_lineage")

    root = Path(output_root)
    destination = (
        root
        / "gistda"
        / "flood_freq"
        / "pattani"
        / run_id
        / transformation_id
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(exist_ok=False)
        pages_directory = destination / "pages"
        pages_directory.mkdir()
    except FileExistsError:
        raise TransformationError("output_already_exists") from None
    except OSError:
        raise TransformationError("publication_failed") from None

    manifest_pages: list[dict[str, object]] = []
    total_features = 0
    for page in sequence:
        transformed = transform_validated_page(
            page.parsed,
            page.validation,
            page.lineage,
        )
        page_name = f"page_{page.lineage.page_index:06d}.jsonl"
        page_path = pages_directory / page_name
        _publish_bytes(page_path, transformed.content)
        total_features += transformed.record_count
        manifest_pages.append({
            "page_index": page.lineage.page_index,
            "requested_offset": page.lineage.requested_offset,
            "requested_limit": page.lineage.requested_limit,
            "source_artifact_path": page.lineage.source_artifact_path,
            "source_metadata_path": page.lineage.source_metadata_path,
            "source_stored_sha256": page.lineage.source_stored_sha256,
            "output_page_path": f"pages/{page_name}",
            "output_page_sha256": transformed.sha256,
            "output_page_byte_count": transformed.byte_count,
            "output_record_count": transformed.record_count,
        })

    manifest = {
        "transformation_schema_version": TRANSFORMATION_SCHEMA_VERSION,
        "transformation_policy_version": TRANSFORMATION_POLICY_VERSION,
        "schema_basis": "versioned_project_schema_from_observed_snapshot",
        "provider_contract_claimed": False,
        "run_id": run_id,
        "transformation_id": transformation_id,
        "status": "complete",
        "page_count": len(sequence),
        "feature_count": total_features,
        "configured_key_check_complete": True,
        "crs_status": "not_verified",
        "geometry_handling": "preserved_without_reprojection_or_srid",
        "attribute_handling": "retained_without_semantic_interpretation",
        "pages": manifest_pages,
    }
    manifest_bytes = _serialize_json(manifest) + b"\n"
    manifest_path = destination / "transformation_manifest.json"
    _publish_bytes(manifest_path, manifest_bytes, completion=True)
    return TransformationResult(
        run_id=run_id,
        transformation_id=transformation_id,
        directory=destination,
        manifest_path=manifest_path,
        page_count=len(sequence),
        feature_count=total_features,
    )


def _validate_feature_schema(feature: dict[str, object]) -> None:
    if set(feature) != _FEATURE_MEMBERS or feature.get("type") != "Feature":
        raise TransformationError("invalid_transformation_schema")
    feature_id = feature.get("id")
    if not isinstance(feature_id, str) or feature_id == "":
        raise TransformationError("invalid_transformation_schema")

    properties = feature.get("properties")
    if not isinstance(properties, dict) or set(properties) != _PROPERTY_MEMBERS:
        raise TransformationError("invalid_transformation_schema")
    if any(type(properties[name]) is not str for name in _STRING_PROPERTIES):
        raise TransformationError("invalid_transformation_schema")
    if any(type(properties[name]) is not int for name in _INTEGER_PROPERTIES):
        raise TransformationError("invalid_transformation_schema")
    for name in _NUMBER_PROPERTIES:
        value = properties[name]
        if type(value) not in {int, float}:
            raise TransformationError("invalid_transformation_schema")
        if type(value) is float and not math.isfinite(value):
            raise TransformationError("invalid_transformation_schema")

    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "MultiPolygon":
        raise TransformationError("invalid_transformation_schema")


def _validate_lineage(
    lineage: SourcePageLineage,
    validation: PageValidationResult,
) -> None:
    if not isinstance(lineage, SourcePageLineage):
        raise TransformationError("invalid_lineage")
    if (
        not _SAFE_IDENTIFIER.fullmatch(lineage.run_id)
        or type(lineage.page_index) is not int
        or lineage.page_index < 0
        or type(lineage.requested_offset) is not int
        or lineage.requested_offset < 0
        or type(lineage.requested_limit) is not int
        or not 1 <= lineage.requested_limit <= 10_000
        or lineage.page_index != validation.page_index
        or lineage.requested_offset != validation.requested_offset
        or lineage.requested_limit != validation.requested_limit
        or not _safe_relative_path(lineage.source_artifact_path)
        or not _safe_relative_path(lineage.source_metadata_path)
        or not _SHA256.fullmatch(lineage.source_stored_sha256)
    ):
        raise TransformationError("invalid_lineage")


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _validate_identifier(value: object) -> None:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        raise TransformationError("invalid_transformation_input")


def _serialize_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        raise TransformationError("serialization_failed") from None


def _publish_bytes(
    destination: Path,
    content: bytes,
    *,
    completion: bool = False,
) -> None:
    temporary: str | None = None
    published = False
    publication_failed = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        published = True
    except OSError:
        publication_failed = True

    cleanup_failed = False
    if temporary is not None:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            cleanup_failed = True

    if publication_failed:
        raise TransformationError("publication_failed") from None
    if published and cleanup_failed:
        raise TransformationError(
            "cleanup_failed",
            record_published=True,
            completion_published=completion,
        ) from None
