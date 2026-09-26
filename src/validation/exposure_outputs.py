"""Read-only verifier for exploratory exposure outputs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class ExposureVerificationResult:
    status: str
    road_count: int | None
    healthcare_count: int | None
    issue_categories: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.status == "complete" and not self.issue_categories


def verify_exposure_output(processed_root: Path, relative_directory: str) -> ExposureVerificationResult:
    issues: set[str] = set(); root = Path(processed_root).resolve()
    if not _relative(relative_directory): return _result(None, None, {"invalid_input"})
    directory = root / PurePosixPath(relative_directory); manifest_path = directory / "analysis_manifest.json"
    try:
        directory.resolve(strict=True).relative_to(root)
        if directory.is_symlink() or not directory.is_dir() or manifest_path.is_symlink(): raise OSError
        raw_manifest = manifest_path.read_bytes()
        manifest = json.loads(raw_manifest, object_pairs_hook=_reject_duplicates)
    except Exception: return _result(None, None, {"manifest_invalid"})
    if (manifest.get("status") != "complete" or manifest.get("schema_version") != "1.0"
            or manifest.get("policy_version") != "exploratory_non_authoritative"):
        issues.add("manifest_invalid")
    outputs = manifest.get("outputs")
    expected_names = {"road_exposure.jsonl", "healthcare_exposure.jsonl", "exposure_summary.json"}
    if not isinstance(outputs, dict) or set(outputs) != expected_names: issues.add("manifest_invalid"); outputs = {}
    allowed = expected_names | {"analysis_manifest.json"}
    try:
        entries = list(directory.iterdir())
        if any(item.name.startswith(".") or item.name.endswith(".tmp") for item in entries): issues.add("temporary_remnant")
        if any(item.name not in allowed for item in entries): issues.add("unexpected_entry")
    except OSError: issues.add("containment_invalid")
    counts: dict[str, int] = {}; exposed_counts: dict[str, int] = {}; summary: dict[str, object] | None = None
    for name, descriptor in outputs.items():
        if not isinstance(descriptor, dict): issues.add("manifest_invalid"); continue
        path = directory / name
        try:
            path.resolve(strict=True).relative_to(directory.resolve())
            if path.is_symlink() or not path.is_file(): raise OSError
            raw = path.read_bytes()
            if len(raw) != descriptor.get("byte_count") or hashlib.sha256(raw).hexdigest() != descriptor.get("sha256"):
                issues.add("integrity_failed")
            if name.endswith(".jsonl"):
                records = [json.loads(line, object_pairs_hook=_reject_duplicates) for line in raw.splitlines()]
                if any(record.get("analysis_sequence") != index or set(record) != {
                    "analysis_sequence", "source_sequence", "intersects_flood", "intersecting_polygon_count"
                } for index, record in enumerate(records)): issues.add("sequence_invalid")
                if any(type(record.get("intersects_flood")) is not bool
                       or type(record.get("intersecting_polygon_count")) is not int
                       or record["intersecting_polygon_count"] < 0
                       or record["intersects_flood"] != (record["intersecting_polygon_count"] > 0)
                       for record in records): issues.add("record_invalid")
                if len(records) != descriptor.get("record_count"): issues.add("count_mismatch")
                counts[name] = len(records)
                exposed_counts[name] = sum(record["intersects_flood"] is True for record in records)
            else:
                summary = json.loads(raw, object_pairs_hook=_reject_duplicates)
                if summary.get("policy_label") != "exploratory_non_authoritative" or summary.get("rejected_or_invalid_input_count") != 0:
                    issues.add("summary_invalid")
        except Exception: issues.add("integrity_failed")
    if isinstance(summary, dict):
        for key, filename in (("roads", "road_exposure.jsonl"), ("healthcare", "healthcare_exposure.jsonl")):
            section = summary.get(key)
            if (not isinstance(section, dict) or section.get("total") != counts.get(filename)
                    or section.get("exposed") != exposed_counts.get(filename)
                    or section.get("non_exposed") != counts.get(filename, 0) - exposed_counts.get(filename, 0)):
                issues.add("summary_invalid")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {"flood", "roads", "healthcare"}:
        issues.add("lineage_invalid")
    else:
        for value in inputs.values():
            if not isinstance(value, dict) or not _relative(value.get("manifest_relative_path")):
                issues.add("lineage_invalid"); continue
            path = root / PurePosixPath(value["manifest_relative_path"])
            try:
                data = path.read_bytes(); path.resolve(strict=True).relative_to(root)
                if hashlib.sha256(data).hexdigest() != value.get("manifest_sha256"): issues.add("lineage_invalid")
            except Exception: issues.add("lineage_invalid")
    return _result(counts.get("road_exposure.jsonl"), counts.get("healthcare_exposure.jsonl"), issues)


def _result(roads: int | None, health: int | None, issues: set[str]) -> ExposureVerificationResult:
    safe = tuple(sorted(issues))
    return ExposureVerificationResult("complete" if not safe else "invalid", roads, health, safe)


def _relative(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value: return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result: raise ValueError
        result[key] = value
    return result
