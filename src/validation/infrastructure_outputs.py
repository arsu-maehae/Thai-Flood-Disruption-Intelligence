"""Read-only verification for completed Phase 3 derived infrastructure outputs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath


_POLICIES = frozenset({
    "pattani-osm-highways-observed-v1",
    "pattani-address-text-candidates-v1",
})
_SAFE_ISSUES = frozenset({
    "containment_invalid", "count_mismatch", "hash_mismatch", "invalid_input",
    "lineage_invalid", "manifest_invalid", "output_incomplete", "record_invalid",
    "temporary_remnant", "unexpected_entry",
})


@dataclass(frozen=True)
class InfrastructureVerificationResult:
    status: str
    output_type: str | None
    record_count: int | None
    byte_count: int | None
    sha256: str | None
    issue_categories: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.status == "complete" and not self.issue_categories

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status, "output_type": self.output_type,
            "record_count": self.record_count, "byte_count": self.byte_count,
            "sha256": self.sha256, "issue_categories": list(self.issue_categories),
        }


def verify_infrastructure_output(
    output_root: Path, relative_directory: str, *, raw_root: Path
) -> InfrastructureVerificationResult:
    issues: set[str] = set()
    root = Path(output_root).resolve()
    if not _safe_relative(relative_directory):
        return _result(None, None, None, None, {"invalid_input"})
    directory = root / PurePosixPath(relative_directory)
    try:
        directory.resolve(strict=False).relative_to(root)
        if directory.is_symlink() or not directory.is_dir():
            return _result(None, None, None, None, {"containment_invalid"})
        entries = list(directory.iterdir())
    except OSError:
        return _result(None, None, None, None, {"containment_invalid"})
    manifest_path = directory / "transformation_manifest.json"
    if not manifest_path.exists():
        manifest_path = directory / "extraction_manifest.json"
    try:
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return _result(None, None, None, None, {"output_incomplete"})
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
    except Exception:
        return _result(None, None, None, None, {"manifest_invalid"})
    policy = manifest.get("policy_version")
    output_type = (
        "roads" if policy == "pattani-osm-highways-observed-v1" else
        "healthcare_candidates" if policy == "pattani-address-text-candidates-v1" else None
    )
    output = manifest.get("output")
    if (manifest.get("status") != "complete" or policy not in _POLICIES
            or manifest.get("schema_version") != "1.0" or not isinstance(output, dict)):
        issues.add("manifest_invalid")
        return _result(output_type, None, None, None, issues)
    relative_path = output.get("relative_path")
    expected_bytes = output.get("byte_count")
    expected_hash = output.get("sha256")
    expected_records = output.get("segment_count") if output_type == "roads" else output.get("record_count")
    if (not _safe_relative(relative_path) or type(expected_bytes) is not int or expected_bytes < 0
            or type(expected_records) is not int or expected_records < 0
            or not isinstance(expected_hash, str) or len(expected_hash) != 64):
        issues.add("manifest_invalid")
        return _result(output_type, None, None, None, issues)
    data_path = root / PurePosixPath(relative_path)
    try:
        data_path.resolve(strict=True).relative_to(root)
        if data_path.parent.resolve() != directory.resolve() or data_path.is_symlink() or not data_path.is_file():
            issues.add("containment_invalid")
    except (OSError, ValueError):
        issues.add("containment_invalid")
    allowed = {manifest_path.name, data_path.name}
    for entry in entries:
        if entry.name.lower().endswith(".tmp") or entry.name.startswith("."):
            issues.add("temporary_remnant")
        elif entry.name not in allowed:
            issues.add("unexpected_entry")
    if issues:
        return _result(output_type, expected_records, expected_bytes, expected_hash, issues)
    digest = hashlib.sha256(); actual_bytes = actual_records = 0
    try:
        with data_path.open("rb") as handle:
            for line in handle:
                digest.update(line); actual_bytes += len(line)
                if not line.endswith(b"\n"):
                    issues.add("record_invalid"); continue
                record = json.loads(line, object_pairs_hook=_reject_duplicates)
                expected_members = ({"feature_sequence", "way_sequence", "segment_sequence", "osm_way_id", "highway", "geometry"}
                                    if output_type == "roads" else {"candidate_sequence", "source_fields"})
                sequence_name = "feature_sequence" if output_type == "roads" else "candidate_sequence"
                if (not isinstance(record, dict) or set(record) != expected_members
                        or record.get(sequence_name) != actual_records):
                    issues.add("record_invalid")
                actual_records += 1
    except Exception:
        issues.add("record_invalid")
    if actual_bytes != expected_bytes or actual_records != expected_records:
        issues.add("count_mismatch")
    if digest.hexdigest() != expected_hash:
        issues.add("hash_mismatch")
    source = manifest.get("source")
    if (not isinstance(source, dict) or not _safe_relative(source.get("relative_artifact_path"))
            or not isinstance(source.get("sha256"), str)):
        issues.add("lineage_invalid")
    else:
        raw = Path(raw_root).resolve()
        source_path = raw / PurePosixPath(str(source["relative_artifact_path"]))
        try:
            source_path.resolve(strict=True).relative_to(raw)
            if source_path.is_symlink() or not source_path.is_file():
                raise OSError
            source_digest = hashlib.sha256(); source_bytes = 0
            with source_path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    source_bytes += len(chunk); source_digest.update(chunk)
            if (source_digest.hexdigest() != source.get("sha256")
                    or source_bytes != source.get("byte_count")):
                issues.add("lineage_invalid")
        except (OSError, ValueError):
            issues.add("lineage_invalid")
    return _result(output_type, expected_records, expected_bytes, expected_hash, issues)


def _result(output_type: str | None, records: int | None, size: int | None,
            digest: str | None, issues: set[str]) -> InfrastructureVerificationResult:
    safe = tuple(sorted(issue if issue in _SAFE_ISSUES else "invalid_input" for issue in issues))
    return InfrastructureVerificationResult("complete" if not safe else "invalid", output_type, records, size, digest, safe)


def _safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value: return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result: raise ValueError("duplicate")
        result[key] = value
    return result
