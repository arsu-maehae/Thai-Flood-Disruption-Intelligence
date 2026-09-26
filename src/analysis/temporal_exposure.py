"""Exploratory non-authoritative annual Pattani exposure aggregation."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Callable, Iterable, Sequence

from shapely.geometry import MultiPolygon, Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from src.analysis import flood_exposure as base
from src.transformation import osm_roads


ANALYSIS_ID = "temporal-exposure-v1-20260926-01"
SCHEMA_VERSION = "1.0"
POLICY_VERSION = "exploratory_non_authoritative"
YEARS = tuple(f"y_{year}" for year in range(2011, 2025))
OUTPUT_RELATIVE = f"analysis/pattani/{ANALYSIS_ID}"
PHASE4A_RELATIVE = base.OUTPUT_RELATIVE
EXPECTED_PHASE4A_ROADS = 4_919
EXPECTED_PHASE4A_HEALTHCARE = 18

_SAFE_ERRORS = frozenset({
    "axis_policy_failed", "cleanup_failed", "frequency_invalid", "geometry_invalid",
    "input_integrity_failed", "invalid_input", "output_exists", "publication_failed",
    "reconciliation_failed", "resource_limit", "schema_invalid", "yearly_field_invalid",
})


class TemporalExposureError(RuntimeError):
    def __init__(self, category: str, *, record_published: bool = False,
                 completion_published: bool = False, cleanup_failed: bool = False) -> None:
        self.category = category if category in _SAFE_ERRORS else "invalid_input"
        self.record_published = record_published is True
        self.completion_published = completion_published is True
        self.cleanup_failed = cleanup_failed is True
        super().__init__(self.category)

    def __repr__(self) -> str:
        return ("TemporalExposureError("
                f"category={self.category!r}, record_published={self.record_published!r}, "
                f"completion_published={self.completion_published!r}, "
                f"cleanup_failed={self.cleanup_failed!r})")


@dataclass(frozen=True)
class FrequencyConsistency:
    feature_count: int
    match_count: int
    mismatch_count: int
    missing_or_invalid_count: int
    per_year_active_counts: tuple[int, ...]


@dataclass(frozen=True)
class TemporalComputation:
    road_masks: tuple[int, ...]
    healthcare_masks: tuple[int, ...]
    frequency: FrequencyConsistency


@dataclass(frozen=True)
class TemporalExposureResult:
    directory: Path
    manifest_path: Path
    flood_count: int
    road_total: int
    road_ever_exposed: int
    healthcare_total: int
    healthcare_ever_exposed: int
    annual_road_counts: tuple[int, ...]
    annual_healthcare_counts: tuple[int, ...]
    output_descriptors: tuple[tuple[str, int, str], ...]


def compute_temporal_exposure(
    flood_geometries: Sequence[MultiPolygon], yearly_flags: Sequence[Sequence[int]],
    frequencies: Sequence[object], road_geometries: Sequence[BaseGeometry],
    healthcare_points: Sequence[Point],
) -> TemporalComputation:
    if not flood_geometries or len(flood_geometries) != len(yearly_flags) or len(flood_geometries) != len(frequencies):
        raise TemporalExposureError("invalid_input")
    masks: list[int] = []; active = [0] * len(YEARS); matches = mismatches = invalid = 0
    for flags, frequency in zip(yearly_flags, frequencies):
        if len(flags) != len(YEARS) or any(type(value) is not int or value not in {0, 1} for value in flags):
            raise TemporalExposureError("yearly_field_invalid")
        mask = sum(value << index for index, value in enumerate(flags)); masks.append(mask)
        for index, value in enumerate(flags): active[index] += value
        if type(frequency) is not int or frequency < 0: invalid += 1
        elif frequency == sum(flags): matches += 1
        else: mismatches += 1
    tree = STRtree(flood_geometries)
    road_masks = tuple(_target_mask(tree, flood_geometries, masks, target) for target in road_geometries)
    health_masks = tuple(_target_mask(tree, flood_geometries, masks, target) for target in healthcare_points)
    return TemporalComputation(road_masks, health_masks,
        FrequencyConsistency(len(flood_geometries), matches, mismatches, invalid, tuple(active)))


def run_temporal_exposure(
    *, raw_root: Path, processed_root: Path, completed_at: datetime | None = None,
    minimum_free_bytes: int = base.MIN_FREE_BYTES,
    minimum_available_memory_bytes: int = base.MIN_AVAILABLE_MEMORY_BYTES,
    memory_probe: Callable[[], int] | None = None,
) -> TemporalExposureResult:
    if (type(minimum_free_bytes) is not int or minimum_free_bytes <= 0
            or type(minimum_available_memory_bytes) is not int or minimum_available_memory_bytes <= 0):
        raise TemporalExposureError("invalid_input")
    timestamp = _timestamp(completed_at or datetime.now(timezone.utc))
    raw = Path(raw_root).resolve(); processed = Path(processed_root).resolve()
    destination = processed / PurePosixPath(OUTPUT_RELATIVE)
    if destination.exists() or destination.is_symlink(): raise TemporalExposureError("output_exists")
    try: destination.resolve(strict=False).relative_to(processed)
    except Exception: raise TemporalExposureError("invalid_input") from None
    try:
        if shutil.disk_usage(processed).free < minimum_free_bytes: raise TemporalExposureError("resource_limit")
    except TemporalExposureError: raise
    except OSError: raise TemporalExposureError("resource_limit") from None
    if (memory_probe or base._available_memory)() < minimum_available_memory_bytes:
        raise TemporalExposureError("resource_limit")

    phase4a = _verify_phase4a(processed)
    flood_dir = processed / PurePosixPath(base.FLOOD_RELATIVE)
    road_dir = processed / PurePosixPath(base.ROAD_RELATIVE)
    health_dir = processed / PurePosixPath(base.HEALTH_RELATIVE)
    flood_manifest, flood_manifest_hash = base._manifest(flood_dir / "transformation_manifest.json")
    road_manifest, road_manifest_hash = base._manifest(road_dir / "extraction_manifest.json")
    health_manifest, health_manifest_hash = base._manifest(health_dir / "transformation_manifest.json")
    roads = base._load_roads(processed, road_manifest)
    normal_points, swapped_points = base._load_health(processed, health_manifest)
    categories = _road_categories(processed, road_manifest, len(roads))

    source = osm_roads.geofabrik_thailand_source(raw); osm_roads._verify_source(source)
    summary = osm_roads._discover_boundary(source.artifact_path)
    osm_roads._require_boundary_contract(summary, osm_roads.PATTANI_BOUNDARY_CONTRACT)
    boundary = osm_roads._assemble_boundary(source.artifact_path)
    osm_roads._require_geometry_contract(boundary, osm_roads.PATTANI_BOUNDARY_CONTRACT)
    if sum(boundary.intersects(point) for point in normal_points) != len(normal_points) or any(boundary.intersects(point) for point in swapped_points):
        raise TemporalExposureError("axis_policy_failed")

    road_masks = [0] * len(roads); health_masks = [0] * len(normal_points)
    active = [0] * len(YEARS); match = mismatch = invalid = flood_count = 0
    for geometries, flags, frequencies in _iter_temporal_pages(flood_dir, raw, flood_manifest):
        computation = compute_temporal_exposure(geometries, flags, frequencies, roads, normal_points)
        road_masks = [left | right for left, right in zip(road_masks, computation.road_masks)]
        health_masks = [left | right for left, right in zip(health_masks, computation.healthcare_masks)]
        frequency = computation.frequency; flood_count += frequency.feature_count
        match += frequency.match_count; mismatch += frequency.mismatch_count; invalid += frequency.missing_or_invalid_count
        active = [left + right for left, right in zip(active, frequency.per_year_active_counts)]
    if flood_count != 112_073: raise TemporalExposureError("reconciliation_failed")
    road_ever = sum(mask != 0 for mask in road_masks); health_ever = sum(mask != 0 for mask in health_masks)
    if (road_ever != EXPECTED_PHASE4A_ROADS or health_ever != EXPECTED_PHASE4A_HEALTHCARE
            or phase4a != (EXPECTED_PHASE4A_ROADS, EXPECTED_PHASE4A_HEALTHCARE)):
        raise TemporalExposureError("reconciliation_failed")
    frequency = FrequencyConsistency(flood_count, match, mismatch, invalid, tuple(active))
    annual_roads = tuple(sum(bool(mask & (1 << index)) for mask in road_masks) for index in range(len(YEARS)))
    annual_health = tuple(sum(bool(mask & (1 << index)) for mask in health_masks) for index in range(len(YEARS)))

    try: destination.parent.mkdir(parents=True, exist_ok=True); destination.mkdir(exist_ok=False)
    except FileExistsError: raise TemporalExposureError("output_exists") from None
    except OSError: raise TemporalExposureError("publication_failed") from None
    published = False; descriptors: dict[str, dict[str, object]] = {}
    try:
        annual_summary = {"policy_label": POLICY_VERSION, "years": list(YEARS),
            "flood_feature_count": flood_count, "road_total": len(roads), "healthcare_total": len(normal_points),
            "road_ever_exposed": road_ever, "healthcare_ever_exposed": health_ever,
            "annual_road_exposed": dict(zip(YEARS, annual_roads)),
            "annual_healthcare_exposed": dict(zip(YEARS, annual_health)),
            "phase4a_reconciled": True,
            "limitations": ["exploratory and non-authoritative", "year flags not semantically interpreted",
                            "not a severity, disruption, risk, or accessibility score"]}
        summary_path = destination / "annual_exposure_summary.json"
        _publish(summary_path, _json(annual_summary) + b"\n"); published = True
        descriptors[summary_path.name] = _descriptor(summary_path, 1)
        category_path = destination / "road_category_annual_exposure.csv"
        category_bytes = _road_category_csv(categories, road_masks)
        _publish(category_path, category_bytes); descriptors[category_path.name] = _descriptor(category_path, len(set(categories)))
        health_path = destination / "healthcare_annual_exposure.csv"
        health_bytes = _health_csv(health_masks)
        _publish(health_path, health_bytes); descriptors[health_path.name] = _descriptor(health_path, 1)
        frequency_path = destination / "frequency_consistency.json"
        frequency_bytes = _json({"feature_count": frequency.feature_count, "match_count": frequency.match_count,
            "mismatch_count": frequency.mismatch_count, "missing_or_invalid_count": frequency.missing_or_invalid_count,
            "per_year_active_feature_counts": dict(zip(YEARS, frequency.per_year_active_counts)),
            "relationship_status": "observed_structural_relationship_only"}) + b"\n"
        _publish(frequency_path, frequency_bytes); descriptors[frequency_path.name] = _descriptor(frequency_path, 1)
        manifest = {"schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION, "status": "complete",
            "analysis_id": ANALYSIS_ID, "completed_at_utc": timestamp,
            "inputs": {"phase4a": _input(processed, PHASE4A_RELATIVE, "analysis_manifest.json"),
                "flood": {"manifest_relative_path": (flood_dir / "transformation_manifest.json").relative_to(processed).as_posix(), "manifest_sha256": flood_manifest_hash, "data_sha256": base._page_digest(flood_manifest)},
                "roads": {"manifest_relative_path": (road_dir / "extraction_manifest.json").relative_to(processed).as_posix(), "manifest_sha256": road_manifest_hash, "data_sha256": road_manifest["output"]["sha256"]},
                "healthcare": {"manifest_relative_path": (health_dir / "transformation_manifest.json").relative_to(processed).as_posix(), "manifest_sha256": health_manifest_hash, "data_sha256": health_manifest["output"]["sha256"]}},
            "outputs": descriptors, "years": list(YEARS),
            "aggregation_rules": {"infrastructure_counted_at_most_once_per_year": True,
                "road_category_is_observed_highway_value": True, "freq_semantics_inferred": False,
                "phase4a_ever_exposure_reconciled": True},
            "spatial_index": "page-bounded Shapely STRtree plus exact intersects and annual bitmask union",
            "resource_policy": {"maximum_flood_features_per_index": 1000,
                "minimum_available_memory_bytes": minimum_available_memory_bytes, "minimum_free_bytes": minimum_free_bytes},
            "limitations": {"exploratory_non_authoritative": True, "official_crs_claimed": False,
                "severity_claimed": False, "disruption_claimed": False, "risk_claimed": False,
                "accessibility_claimed": False, "completeness_claimed": False, "field_semantics_claimed": False},
            "tools": {"shapely": osm_roads._package_version("shapely")}}
        manifest_path = destination / "analysis_manifest.json"
        _publish(manifest_path, _json(manifest) + b"\n", completion=True)
        return TemporalExposureResult(destination, manifest_path, flood_count, len(roads), road_ever,
            len(normal_points), health_ever, annual_roads, annual_health,
            tuple((name, int(value["byte_count"]), str(value["sha256"])) for name, value in sorted(descriptors.items())))
    except TemporalExposureError: raise
    except Exception: raise TemporalExposureError("publication_failed", record_published=published) from None


def _iter_temporal_pages(directory: Path, raw: Path, manifest: dict[str, object]) -> Iterable[tuple[list[MultiPolygon], list[tuple[int, ...]], list[object]]]:
    pages = manifest.get("pages")
    if not isinstance(pages, list): raise TemporalExposureError("schema_invalid")
    validated = base._iter_flood_pages(directory, raw, manifest)
    processed_pages = 0
    for page_index, geometries in enumerate(validated):
        if page_index >= len(pages): raise TemporalExposureError("reconciliation_failed")
        page = pages[page_index]; processed_pages += 1
        path = directory / PurePosixPath(str(page["output_page_path"])); flags: list[tuple[int, ...]] = []; frequencies: list[object] = []
        with path.open("rb") as handle:
            for line in handle:
                try: properties = json.loads(line, object_pairs_hook=_reject_duplicates)["feature"]["properties"]
                except Exception: raise TemporalExposureError("schema_invalid") from None
                if not isinstance(properties, dict): raise TemporalExposureError("schema_invalid")
                values: list[int] = []
                for year in YEARS:
                    value = properties.get(year)
                    if type(value) is not int or value not in {0, 1}: raise TemporalExposureError("yearly_field_invalid")
                    values.append(value)
                flags.append(tuple(values)); frequencies.append(properties.get("freq"))
        if len(flags) != len(geometries): raise TemporalExposureError("reconciliation_failed")
        yield geometries, flags, frequencies
    if processed_pages != len(pages): raise TemporalExposureError("reconciliation_failed")


def _road_categories(root: Path, manifest: dict[str, object], count: int) -> list[str]:
    path = root / PurePosixPath(str(manifest["output"]["relative_path"])); categories: list[str] = []
    with path.open("rb") as handle:
        for line in handle:
            try: value = json.loads(line, object_pairs_hook=_reject_duplicates)["highway"]
            except Exception: raise TemporalExposureError("schema_invalid") from None
            if not isinstance(value, str) or not value: raise TemporalExposureError("schema_invalid")
            categories.append(value)
    if len(categories) != count: raise TemporalExposureError("reconciliation_failed")
    return categories


def _verify_phase4a(root: Path) -> tuple[int, int]:
    from src.validation.exposure_outputs import verify_exposure_output
    result = verify_exposure_output(root, PHASE4A_RELATIVE)
    if not result.complete: raise TemporalExposureError("input_integrity_failed")
    summary = json.loads((root / PHASE4A_RELATIVE / "exposure_summary.json").read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
    return int(summary["roads"]["exposed"]), int(summary["healthcare"]["exposed"])


def _target_mask(tree: STRtree, geometries: Sequence[MultiPolygon], masks: Sequence[int], target: BaseGeometry) -> int:
    result = 0
    for index in tree.query(target):
        position = int(index)
        if geometries[position].intersects(target): result |= masks[position]
    return result


def _road_category_csv(categories: Sequence[str], masks: Sequence[int]) -> bytes:
    totals: dict[str, list[int]] = {}
    for category, mask in zip(categories, masks):
        row = totals.setdefault(category, [0] * (len(YEARS) + 2)); row[-1] += 1; row[-2] += mask != 0
        for index in range(len(YEARS)): row[index] += bool(mask & (1 << index))
    output = io.StringIO(newline=""); writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["highway_category", *YEARS, "ever_exposed", "total_records"])
    for category in sorted(totals): writer.writerow([category, *totals[category]])
    return output.getvalue().encode("utf-8")


def _health_csv(masks: Sequence[int]) -> bytes:
    annual = [sum(bool(mask & (1 << index)) for mask in masks) for index in range(len(YEARS))]
    output = io.StringIO(newline=""); writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["scope", *YEARS, "ever_exposed", "total_records"])
    writer.writerow(["address_text_candidates", *annual, sum(mask != 0 for mask in masks), len(masks)])
    return output.getvalue().encode("utf-8")


def _input(root: Path, relative: str, manifest_name: str) -> dict[str, str]:
    path = root / relative / manifest_name; raw = path.read_bytes()
    return {"manifest_relative_path": path.relative_to(root).as_posix(), "manifest_sha256": hashlib.sha256(raw).hexdigest()}


def _descriptor(path: Path, records: int) -> dict[str, object]:
    raw = path.read_bytes(); return {"relative_path": path.name, "byte_count": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(), "record_count": records}


def _json(value: object) -> bytes:
    try: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except Exception: raise TemporalExposureError("schema_invalid") from None


def _publish(path: Path, content: bytes, *, completion: bool = False) -> None:
    temporary: Path | None = None; linked = False
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name); handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.link(temporary, path); linked = True; temporary.unlink(); temporary = None
    except FileExistsError:
        failed = not _cleanup(temporary)
        raise TemporalExposureError("cleanup_failed" if failed else "output_exists", record_published=linked,
            completion_published=linked and completion, cleanup_failed=failed) from None
    except OSError:
        failed = not _cleanup(temporary)
        raise TemporalExposureError("cleanup_failed" if linked else "publication_failed",
            record_published=linked, completion_published=linked and completion, cleanup_failed=failed) from None


def _cleanup(path: Path | None) -> bool:
    if path is None: return True
    try: path.unlink(missing_ok=True); return True
    except OSError: return False


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None: raise TemporalExposureError("invalid_input")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result: raise ValueError
        result[key] = value
    return result
