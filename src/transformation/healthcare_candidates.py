"""Deterministic offline extraction of Pattani address-text healthcare candidates."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata


HEALTHCARE_SCHEMA_VERSION = "1.0"
HEALTHCARE_POLICY_VERSION = "pattani-address-text-candidates-v1"
DEFAULT_TRANSFORMATION_ID = "pattani-address-candidates-v1-20260925-01"
SOURCE_RESOURCE_ID = "2d45b0c6-75e9-4463-888d-ef364ad164fb"
SOURCE_SHA256 = "fede0061abf49d81047046f4fea2a26efad2b33c2237b1a07543c0230e31b60b"
SOURCE_BYTES = 4_857_492
EXPECTED_SOURCE_RECORDS = 10_714
EXPECTED_CANDIDATES = 138
TARGET_TEXT = "\u0e1b\u0e31\u0e15\u0e15\u0e32\u0e19\u0e35"
ID_FIELD = "\u0e23\u0e2b\u0e31\u0e2a\u0e2b\u0e19\u0e48\u0e27\u0e22\u0e07\u0e32\u0e19"
ADDRESS_FIELD = "\u0e17\u0e35\u0e48\u0e2d\u0e22\u0e39\u0e48\u0e08\u0e38\u0e14\u0e1a\u0e23\u0e34\u0e01\u0e32\u0e23"
LATITUDE_FIELD = "\u0e25\u0e30\u0e15\u0e34\u0e08\u0e39\u0e14"
LONGITUDE_FIELD = "\u0e25\u0e2d\u0e07\u0e15\u0e34\u0e08\u0e39\u0e14"
EXPECTED_FIELDS = (
    ID_FIELD,
    "\u0e01\u0e23\u0e30\u0e17\u0e23\u0e27\u0e07",
    "\u0e01\u0e23\u0e21",
    "\u0e2b\u0e19\u0e48\u0e27\u0e22\u0e07\u0e32\u0e19",
    "\u0e2b\u0e21\u0e38\u0e14\u0e17\u0e35\u0e48\u0e44\u0e21\u0e48\u0e1e\u0e1a\u0e04\u0e27\u0e32\u0e21\u0e1c\u0e34\u0e14\u0e1b\u0e01\u0e15\u0e34",
    ADDRESS_FIELD,
    LATITUDE_FIELD,
    LONGITUDE_FIELD,
)

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_ERRORS = frozenset({
    "cleanup_failed", "invalid_input", "output_already_exists",
    "publication_failed", "schema_drift", "source_integrity_failed",
    "validation_failed",
})


class HealthcareTransformationError(RuntimeError):
    def __init__(self, category: str, *, data_published: bool = False,
                 manifest_published: bool = False, cleanup_failed: bool = False) -> None:
        self.category = category if category in _SAFE_ERRORS else "invalid_input"
        self.data_published = data_published is True
        self.manifest_published = manifest_published is True
        self.cleanup_failed = cleanup_failed is True
        super().__init__(self.category)

    def __repr__(self) -> str:
        return ("HealthcareTransformationError("
                f"category={self.category!r}, data_published={self.data_published!r}, "
                f"manifest_published={self.manifest_published!r}, "
                f"cleanup_failed={self.cleanup_failed!r})")


@dataclass(frozen=True)
class HealthcareTransformationResult:
    transformation_id: str
    directory: Path
    data_path: Path
    manifest_path: Path
    source_record_count: int
    candidate_count: int
    byte_count: int
    sha256: str


def dga_healthcare_source(raw_root: Path) -> tuple[Path, Path]:
    stem = f"resource-{SOURCE_RESOURCE_ID}__sha256-{SOURCE_SHA256}"
    directory = Path(raw_root).resolve() / "infrastructure/healthcare/dga"
    return directory / f"{stem}.csv", directory / f"{stem}.metadata.json"


def extract_pattani_healthcare_candidates(
    *, raw_root: Path, output_root: Path,
    transformation_id: str = DEFAULT_TRANSFORMATION_ID,
    minimum_free_bytes: int = 1_073_741_824,
) -> HealthcareTransformationResult:
    if (not isinstance(transformation_id, str) or not _SAFE_ID.fullmatch(transformation_id)
            or type(minimum_free_bytes) is not int or minimum_free_bytes <= 0):
        raise HealthcareTransformationError("invalid_input")
    artifact, metadata_path = dga_healthcare_source(raw_root)
    metadata = _verify_source(artifact, metadata_path, Path(raw_root).resolve())
    root = Path(output_root).resolve()
    destination = root / "infrastructure/healthcare/dga" / transformation_id
    _require_contained(root, destination)
    if destination.exists() or destination.is_symlink():
        raise HealthcareTransformationError("output_already_exists")
    try:
        if shutil.disk_usage(_existing_parent(root)).free < minimum_free_bytes:
            raise HealthcareTransformationError("invalid_input")
    except HealthcareTransformationError:
        raise
    except OSError:
        raise HealthcareTransformationError("invalid_input") from None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(exist_ok=False)
    except FileExistsError:
        raise HealthcareTransformationError("output_already_exists") from None
    except OSError:
        raise HealthcareTransformationError("publication_failed") from None

    temporary: Path | None = None
    data_published = manifest_published = False
    try:
        temporary, summary = _write_candidates(artifact, destination)
        data_path = destination / f"pattani_healthcare_candidates__sha256-{summary['sha256']}.jsonl"
        _link_no_replace(temporary, data_path)
        data_published = True
        _cleanup_one(temporary, data_published=True)
        temporary = None
        manifest = {
            "schema_version": HEALTHCARE_SCHEMA_VERSION,
            "policy_version": HEALTHCARE_POLICY_VERSION,
            "status": "complete",
            "transformation_id": transformation_id,
            "classification": "address_text_candidates",
            "source": {
                "resource_id": SOURCE_RESOURCE_ID,
                "relative_artifact_path": metadata["relative_artifact_path"],
                "byte_count": SOURCE_BYTES,
                "sha256": SOURCE_SHA256,
                "record_count": summary["source_record_count"],
            },
            "selection": {
                "normalization": "Unicode NFC",
                "method": "literal address substring",
                "candidate_count": summary["candidate_count"],
                "distinct_nonempty_identifier_count": summary["distinct_identifier_count"],
                "duplicate_candidate_row_count": summary["duplicate_row_count"],
                "finite_coordinate_pair_count": summary["finite_coordinate_pair_count"],
                "facility_status_claimed": False,
                "coverage_claimed": False,
                "crs_inferred": False,
            },
            "output": {
                "relative_path": data_path.relative_to(root).as_posix(),
                "byte_count": summary["byte_count"],
                "sha256": summary["sha256"],
                "record_count": summary["candidate_count"],
                "record_members": ["candidate_sequence", "source_fields"],
            },
        }
        manifest_bytes = _json_bytes(manifest) + b"\n"
        manifest_path = destination / "transformation_manifest.json"
        temporary = _write_temporary(destination, ".manifest.", manifest_bytes)
        _link_no_replace(temporary, manifest_path)
        manifest_published = True
        _cleanup_one(temporary, data_published=True, manifest_published=True)
        temporary = None
        return HealthcareTransformationResult(
            transformation_id, destination, data_path, manifest_path,
            int(summary["source_record_count"]), int(summary["candidate_count"]),
            int(summary["byte_count"]), str(summary["sha256"]),
        )
    except HealthcareTransformationError as error:
        cleanup_failed = not _cleanup_paths(temporary)
        raise HealthcareTransformationError(
            error.category, data_published=data_published or error.data_published,
            manifest_published=manifest_published or error.manifest_published,
            cleanup_failed=cleanup_failed or error.cleanup_failed,
        ) from None
    except Exception:
        cleanup_failed = not _cleanup_paths(temporary)
        raise HealthcareTransformationError(
            "validation_failed", data_published=data_published,
            manifest_published=manifest_published, cleanup_failed=cleanup_failed,
        ) from None


def _write_candidates(artifact: Path, destination: Path) -> tuple[Path, dict[str, object]]:
    temporary: Path | None = None
    handle = None
    identifiers: set[str] = set()
    row_hashes: set[str] = set()
    source_count = candidate_count = duplicate_count = finite_pairs = byte_count = 0
    digest = hashlib.sha256()
    try:
        handle = tempfile.NamedTemporaryFile("wb", dir=destination, prefix=".healthcare.", suffix=".tmp", delete=False)
        temporary = Path(handle.name)
        with artifact.open("r", encoding="utf-8-sig", errors="strict", newline="") as source:
            reader = csv.DictReader(source)
            if tuple(reader.fieldnames or ()) != EXPECTED_FIELDS:
                raise HealthcareTransformationError("schema_drift")
            for row in reader:
                source_count += 1
                if None in row or set(row) != set(EXPECTED_FIELDS):
                    raise HealthcareTransformationError("schema_drift")
                normalized = {key: unicodedata.normalize("NFC", value) for key, value in row.items()}
                if TARGET_TEXT not in normalized[ADDRESS_FIELD]:
                    continue
                identifier = normalized[ID_FIELD]
                if not identifier or identifier in identifiers:
                    raise HealthcareTransformationError("validation_failed")
                identifiers.add(identifier)
                row_bytes = _json_bytes(normalized)
                row_digest = hashlib.sha256(row_bytes).hexdigest()
                duplicate_count += row_digest in row_hashes
                row_hashes.add(row_digest)
                if _finite_number(normalized[LATITUDE_FIELD]) and _finite_number(normalized[LONGITUDE_FIELD]):
                    finite_pairs += 1
                line = _json_bytes({"candidate_sequence": candidate_count, "source_fields": normalized}) + b"\n"
                handle.write(line); digest.update(line); byte_count += len(line); candidate_count += 1
        if (source_count != EXPECTED_SOURCE_RECORDS or candidate_count != EXPECTED_CANDIDATES
                or len(identifiers) != EXPECTED_CANDIDATES or duplicate_count != 0
                or finite_pairs != EXPECTED_CANDIDATES):
            raise HealthcareTransformationError("validation_failed")
        handle.flush(); os.fsync(handle.fileno()); handle.close(); handle = None
        return temporary, {"source_record_count": source_count, "candidate_count": candidate_count,
            "distinct_identifier_count": len(identifiers), "duplicate_row_count": duplicate_count,
            "finite_coordinate_pair_count": finite_pairs, "byte_count": byte_count,
            "sha256": digest.hexdigest()}
    except HealthcareTransformationError:
        if handle is not None:
            try: handle.close()
            except OSError: pass
        if not _cleanup_paths(temporary):
            raise HealthcareTransformationError("cleanup_failed", cleanup_failed=True) from None
        raise
    except Exception:
        if handle is not None:
            try: handle.close()
            except OSError: pass
        cleanup_failed = not _cleanup_paths(temporary)
        raise HealthcareTransformationError("validation_failed", cleanup_failed=cleanup_failed) from None


def _verify_source(artifact: Path, metadata_path: Path, root: Path) -> dict[str, object]:
    try:
        _require_contained(root, artifact); _require_contained(root, metadata_path)
        if (not artifact.is_file() or artifact.is_symlink() or not metadata_path.is_file()
                or metadata_path.is_symlink()):
            raise HealthcareTransformationError("source_integrity_failed")
        raw_digest = hashlib.sha256(); total = 0
        with artifact.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                total += len(chunk); raw_digest.update(chunk)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
        if (total != SOURCE_BYTES or raw_digest.hexdigest() != SOURCE_SHA256
                or metadata.get("byte_count") != SOURCE_BYTES or metadata.get("sha256") != SOURCE_SHA256
                or metadata.get("resource_id") != SOURCE_RESOURCE_ID
                or not _safe_relative(metadata.get("relative_artifact_path"))
                or (root / PurePosixPath(str(metadata["relative_artifact_path"]))).resolve() != artifact.resolve()):
            raise HealthcareTransformationError("source_integrity_failed")
        return metadata
    except HealthcareTransformationError: raise
    except Exception: raise HealthcareTransformationError("source_integrity_failed") from None


def _finite_number(value: str) -> bool:
    try: return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError): return False


def _json_bytes(value: object) -> bytes:
    try: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except Exception: raise HealthcareTransformationError("validation_failed") from None


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result: raise ValueError("duplicate")
        result[key] = value
    return result


def _safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value: return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path != path.parent: path = path.parent
    return path


def _require_contained(root: Path, destination: Path) -> None:
    try:
        destination.resolve(strict=False).relative_to(root)
        current = root; reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        for part in destination.relative_to(root).parts:
            current /= part
            if current.exists() or current.is_symlink():
                details = current.lstat()
                if stat.S_ISLNK(details.st_mode) or bool(getattr(details, "st_file_attributes", 0) & reparse):
                    raise HealthcareTransformationError("invalid_input")
    except HealthcareTransformationError: raise
    except Exception: raise HealthcareTransformationError("invalid_input") from None


def _write_temporary(directory: Path, prefix: str, content: bytes) -> Path:
    path = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=directory, prefix=prefix, suffix=".tmp", delete=False) as handle:
            path = Path(handle.name); handle.write(content); handle.flush(); os.fsync(handle.fileno())
        return path
    except OSError:
        failed = not _cleanup_paths(path)
        raise HealthcareTransformationError("publication_failed", cleanup_failed=failed) from None


def _link_no_replace(source: Path, destination: Path) -> None:
    try: os.link(source, destination)
    except FileExistsError: raise HealthcareTransformationError("output_already_exists") from None
    except OSError: raise HealthcareTransformationError("publication_failed") from None


def _cleanup_one(path: Path, *, data_published: bool, manifest_published: bool = False) -> None:
    if not _cleanup_paths(path):
        raise HealthcareTransformationError("cleanup_failed", data_published=data_published,
                                             manifest_published=manifest_published, cleanup_failed=True)


def _cleanup_paths(*paths: Path | None) -> bool:
    success = True
    for path in paths:
        if path is not None:
            try: path.unlink(missing_ok=True)
            except OSError: success = False
    return success
