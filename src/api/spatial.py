"""Verified, bounded spatial payload for the local exploratory dashboard."""

from __future__ import annotations

import json
import math
from pathlib import Path, PurePosixPath
from typing import Iterable

from src.transformation.healthcare_candidates import LATITUDE_FIELD, LONGITUDE_FIELD
from src.validation.exposure_outputs import verify_exposure_output
from src.validation.infrastructure_outputs import verify_infrastructure_output


REPORT_ID = "exploratory-exposure-report-v1-20260927-01"
MAX_ROAD_FEATURES = 10_000
MAX_HEALTHCARE_FEATURES = 500
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
ROAD_DIRECTORY = "infrastructure/roads/osm/pattani/pattani-osm-highways-v1-20260925-01"
HEALTHCARE_DIRECTORY = "infrastructure/healthcare/dga/pattani-address-candidates-v1-20260925-01"
EXPOSURE_DIRECTORY = "analysis/pattani/exploratory-exposure-v1-20260925-01"

_SAFE_CATEGORIES = frozenset({"invalid_report", "payload_invalid", "payload_unavailable", "payload_limit_exceeded"})


class SpatialPayloadError(RuntimeError):
    """Fixed-category error which never exposes source values or paths."""

    def __init__(self, category: str) -> None:
        self.category = category if category in _SAFE_CATEGORIES else "payload_invalid"
        super().__init__(self.category)

    def __repr__(self) -> str:
        return f"SpatialPayloadError(category={self.category!r})"


class SpatialPayloadStore:
    """Read and verify the fixed Phase 4 snapshot, then return a bounded view."""

    def __init__(self, processed_root: Path, raw_root: Path) -> None:
        self._processed_root = Path(processed_root)
        self._raw_root = Path(raw_root)
        self._cached: dict[str, object] | None = None

    def payload(self, report_id: str) -> dict[str, object]:
        if report_id != REPORT_ID:
            raise SpatialPayloadError("invalid_report")
        if self._cached is None:
            self._cached = self._load()
        return self._cached

    def _load(self) -> dict[str, object]:
        try:
            for relative in (ROAD_DIRECTORY, HEALTHCARE_DIRECTORY):
                result = verify_infrastructure_output(
                    self._processed_root, relative, raw_root=self._raw_root
                )
                if not result.complete:
                    raise SpatialPayloadError("payload_unavailable")
            exposure = verify_exposure_output(self._processed_root, EXPOSURE_DIRECTORY)
            if not exposure.complete:
                raise SpatialPayloadError("payload_unavailable")
            roads = _read_output(self._processed_root, ROAD_DIRECTORY)
            healthcare = _read_output(self._processed_root, HEALTHCARE_DIRECTORY)
            road_exposure = _read_jsonl(self._processed_root / EXPOSURE_DIRECTORY / "road_exposure.jsonl")
            health_exposure = _read_jsonl(self._processed_root / EXPOSURE_DIRECTORY / "healthcare_exposure.jsonl")
            payload = build_spatial_payload(roads, road_exposure, healthcare, health_exposure)
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            if len(encoded) > MAX_PAYLOAD_BYTES:
                raise SpatialPayloadError("payload_limit_exceeded")
            return payload
        except SpatialPayloadError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            raise SpatialPayloadError("payload_invalid") from None


def build_spatial_payload(
    road_records: list[dict[str, object]],
    road_exposure: list[dict[str, object]],
    healthcare_records: list[dict[str, object]],
    healthcare_exposure: list[dict[str, object]],
) -> dict[str, object]:
    """Build a deterministic bounded payload from already-validated outputs."""
    if len(road_records) != len(road_exposure) or len(healthcare_records) != len(healthcare_exposure):
        raise SpatialPayloadError("payload_invalid")
    road_items: list[dict[str, object]] = []
    for index, (source, exposure) in enumerate(zip(road_records, road_exposure, strict=True)):
        if source.get("feature_sequence") != index or exposure.get("source_sequence") != index:
            raise SpatialPayloadError("payload_invalid")
        category = source.get("highway")
        geometry = source.get("geometry")
        exposed = exposure.get("intersects_flood")
        if not isinstance(category, str) or not category or type(exposed) is not bool:
            raise SpatialPayloadError("payload_invalid")
        _validate_geometry(geometry, {"LineString", "MultiLineString"})
        road_items.append({"exposed": exposed, "geometry": geometry})

    exposed_indexes = [index for index, item in enumerate(road_items) if item["exposed"] is True]
    other_indexes = [index for index, item in enumerate(road_items) if item["exposed"] is False]
    selected_indexes = _even_sample(exposed_indexes, min(len(exposed_indexes), MAX_ROAD_FEATURES))
    remaining = max(0, MAX_ROAD_FEATURES - len(selected_indexes))
    selected_indexes.extend(_even_sample(other_indexes, remaining))
    selected = [road_items[index] for index in sorted(selected_indexes)]

    health_items: list[dict[str, object]] = []
    for index, (source, exposure) in enumerate(zip(healthcare_records, healthcare_exposure, strict=True)):
        if source.get("candidate_sequence") != index or exposure.get("source_sequence") != index:
            raise SpatialPayloadError("payload_invalid")
        fields = source.get("source_fields")
        exposed = exposure.get("intersects_flood")
        if not isinstance(fields, dict) or type(exposed) is not bool:
            raise SpatialPayloadError("payload_invalid")
        try:
            point = [float(fields[LONGITUDE_FIELD]), float(fields[LATITUDE_FIELD])]
        except (KeyError, TypeError, ValueError):
            raise SpatialPayloadError("payload_invalid") from None
        if not all(math.isfinite(value) for value in point):
            raise SpatialPayloadError("payload_invalid")
        health_items.append({"exposed": exposed, "geometry": {"type": "Point", "coordinates": point}})
    if len(health_items) > MAX_HEALTHCARE_FEATURES:
        raise SpatialPayloadError("payload_limit_exceeded")

    bounds = _bounds(selected, health_items)
    return {
        "report_id": REPORT_ID,
        "policy_label": "exploratory_non_authoritative",
        "coordinate_policy": "exploratory_rfc7946_position_interpretation",
        "year_filtering": "unavailable_without_persisted_feature_level_annual_lineage",
        "limits": {
            "maximum_road_features": MAX_ROAD_FEATURES,
            "maximum_healthcare_features": MAX_HEALTHCARE_FEATURES,
            "maximum_payload_bytes": MAX_PAYLOAD_BYTES,
        },
        "extent": bounds,
        "roads": {
            "metadata": {
                "population_counts": {
                    "total": len(road_items),
                    "exposed": len(exposed_indexes),
                    "non_exposed": len(other_indexes),
                },
                "displayed_counts": {
                    "total": len(selected),
                    "exposed": sum(item["exposed"] is True for item in selected),
                    "non_exposed": sum(item["exposed"] is False for item in selected),
                },
                "deterministic_selection_policy": "all_exposed_then_evenly_spaced_non_exposed_context",
                "representative_sample": False,
                "prevalence_inference_allowed": False,
            },
            "truncated": len(selected) < len(road_items),
            "features": selected,
        },
        "healthcare": {
            "total_count": len(health_items),
            "total_exposed_count": sum(item["exposed"] is True for item in health_items),
            "returned_count": len(health_items),
            "truncated": False,
            "features": health_items,
        },
        "caveats": [
            "Geometric intersection is not confirmed disruption, risk, damage, accessibility, or completeness.",
            "Provider CRS is unverified; positions use the reviewed exploratory RFC 7946 interpretation.",
            "Flood polygons and source identifiers are not included in this browser payload.",
        ],
    }


def _read_output(root: Path, relative_directory: str) -> list[dict[str, object]]:
    directory = root / PurePosixPath(relative_directory)
    manifest_name = "extraction_manifest.json" if "roads" in relative_directory else "transformation_manifest.json"
    manifest = json.loads((directory / manifest_name).read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
    relative_path = manifest["output"]["relative_path"]
    if not isinstance(relative_path, str):
        raise ValueError
    return _read_jsonl(root / PurePosixPath(relative_path))


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            value = json.loads(line, object_pairs_hook=_reject_duplicates)
            if not isinstance(value, dict):
                raise ValueError
            records.append(value)
    return records


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _even_sample(items: list, count: int) -> list:
    if count >= len(items):
        return list(items)
    if count <= 0:
        return []
    return [items[(index * len(items)) // count] for index in range(count)]


def _validate_geometry(value: object, allowed: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != {"type", "coordinates"} or value.get("type") not in allowed:
        raise SpatialPayloadError("payload_invalid")
    seen = 0
    for coordinate in _coordinate_pairs(value.get("coordinates")):
        if len(coordinate) < 2 or not all(type(item) in {int, float} and math.isfinite(item) for item in coordinate[:2]):
            raise SpatialPayloadError("payload_invalid")
        seen += 1
    if seen < 2:
        raise SpatialPayloadError("payload_invalid")


def _coordinate_pairs(value: object) -> Iterable[list[float]]:
    if isinstance(value, list) and len(value) >= 2 and all(type(item) in {int, float} for item in value[:2]):
        yield value
    elif isinstance(value, list):
        for child in value:
            yield from _coordinate_pairs(child)
    else:
        raise SpatialPayloadError("payload_invalid")


def _bounds(roads: list[dict[str, object]], healthcare: list[dict[str, object]]) -> list[float]:
    coordinates: list[list[float]] = []
    for item in roads:
        coordinates.extend(_coordinate_pairs(item["geometry"]["coordinates"]))  # type: ignore[index]
    for item in healthcare:
        coordinates.append(item["geometry"]["coordinates"])  # type: ignore[index]
    if not coordinates:
        raise SpatialPayloadError("payload_invalid")
    xs = [pair[0] for pair in coordinates]
    ys = [pair[1] for pair in coordinates]
    bounds = [min(xs), min(ys), max(xs), max(ys)]
    if not all(math.isfinite(value) for value in bounds) or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise SpatialPayloadError("payload_invalid")
    return bounds


__all__ = [
    "MAX_HEALTHCARE_FEATURES", "MAX_PAYLOAD_BYTES", "MAX_ROAD_FEATURES", "REPORT_ID",
    "SpatialPayloadError", "SpatialPayloadStore", "build_spatial_payload",
]
