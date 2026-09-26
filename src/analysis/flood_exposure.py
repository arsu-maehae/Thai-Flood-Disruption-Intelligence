"""Non-authoritative offline flood intersection analysis for Pattani."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Callable, Iterable, Sequence

from shapely import get_coordinates
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree

from src.transformation.healthcare_candidates import EXPECTED_FIELDS, LATITUDE_FIELD, LONGITUDE_FIELD
from src.transformation import osm_roads


ANALYSIS_ID = "exploratory-exposure-v1-20260925-01"
SCHEMA_VERSION = "1.0"
POLICY_VERSION = "exploratory_non_authoritative"
FLOOD_RELATIVE = "gistda/flood_freq/pattani/pattani-full-20260922-01/neutral-jsonl-v1-20260923-01"
ROAD_RELATIVE = "infrastructure/roads/osm/pattani/pattani-osm-highways-v1-20260925-01"
HEALTH_RELATIVE = "infrastructure/healthcare/dga/pattani-address-candidates-v1-20260925-01"
OUTPUT_RELATIVE = f"analysis/pattani/{ANALYSIS_ID}"
MAX_FLOOD_FEATURES = 200_000
MAX_ROAD_RECORDS = 100_000
MAX_HEALTH_RECORDS = 10_000
MIN_AVAILABLE_MEMORY_BYTES = 512 * 1024 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024

_SAFE_ERRORS = frozenset({
    "axis_policy_failed", "cleanup_failed", "geometry_invalid", "input_integrity_failed",
    "invalid_input", "output_exists", "publication_failed", "resource_limit",
    "schema_invalid",
})


class ExposureAnalysisError(RuntimeError):
    def __init__(self, category: str, *, record_published: bool = False,
                 completion_published: bool = False, cleanup_failed: bool = False) -> None:
        self.category = category if category in _SAFE_ERRORS else "invalid_input"
        self.record_published = record_published is True
        self.completion_published = completion_published is True
        self.cleanup_failed = cleanup_failed is True
        super().__init__(self.category)

    def __repr__(self) -> str:
        return ("ExposureAnalysisError("
                f"category={self.category!r}, record_published={self.record_published!r}, "
                f"completion_published={self.completion_published!r}, "
                f"cleanup_failed={self.cleanup_failed!r})")


@dataclass(frozen=True)
class ExposureComputation:
    road_counts: tuple[int, ...]
    healthcare_counts: tuple[int, ...]
    normal_axis_inside_count: int
    swapped_axis_inside_count: int


@dataclass(frozen=True)
class ExposureAnalysisResult:
    directory: Path
    manifest_path: Path
    flood_count: int
    road_total: int
    road_exposed: int
    healthcare_total: int
    healthcare_exposed: int
    output_descriptors: tuple[tuple[str, int, str], ...]


def compute_exposure(
    flood_geometries: Sequence[MultiPolygon], road_geometries: Sequence[BaseGeometry],
    normal_points: Sequence[Point], swapped_points: Sequence[Point],
    pattani_boundary: MultiPolygon,
) -> ExposureComputation:
    if (not flood_geometries or len(normal_points) != len(swapped_points)
            or not isinstance(pattani_boundary, MultiPolygon) or not pattani_boundary.is_valid):
        raise ExposureAnalysisError("invalid_input")
    tree = STRtree(flood_geometries)
    road_counts = tuple(_intersection_count(tree, flood_geometries, item) for item in road_geometries)
    health_counts = tuple(_intersection_count(tree, flood_geometries, item) for item in normal_points)
    normal_inside = sum(pattani_boundary.intersects(item) for item in normal_points)
    swapped_inside = sum(pattani_boundary.intersects(item) for item in swapped_points)
    return ExposureComputation(road_counts, health_counts, normal_inside, swapped_inside)


def run_exploratory_exposure(
    *, raw_root: Path, processed_root: Path,
    completed_at: datetime | None = None,
    minimum_free_bytes: int = MIN_FREE_BYTES,
    minimum_available_memory_bytes: int = MIN_AVAILABLE_MEMORY_BYTES,
    memory_probe: Callable[[], int] | None = None,
) -> ExposureAnalysisResult:
    if (type(minimum_free_bytes) is not int or minimum_free_bytes <= 0
            or type(minimum_available_memory_bytes) is not int or minimum_available_memory_bytes <= 0):
        raise ExposureAnalysisError("invalid_input")
    timestamp = _timestamp(completed_at or datetime.now(timezone.utc))
    raw = Path(raw_root).resolve(); processed = Path(processed_root).resolve()
    destination = processed / PurePosixPath(OUTPUT_RELATIVE)
    _contained(processed, destination)
    if destination.exists() or destination.is_symlink():
        raise ExposureAnalysisError("output_exists")
    try:
        if shutil.disk_usage(_existing_parent(processed)).free < minimum_free_bytes:
            raise ExposureAnalysisError("resource_limit")
    except ExposureAnalysisError: raise
    except OSError: raise ExposureAnalysisError("resource_limit") from None
    if (memory_probe or _available_memory)() < minimum_available_memory_bytes:
        raise ExposureAnalysisError("resource_limit")

    flood_dir = processed / PurePosixPath(FLOOD_RELATIVE)
    road_dir = processed / PurePosixPath(ROAD_RELATIVE)
    health_dir = processed / PurePosixPath(HEALTH_RELATIVE)
    flood_manifest, flood_manifest_hash = _manifest(flood_dir / "transformation_manifest.json")
    road_manifest, road_manifest_hash = _manifest(road_dir / "extraction_manifest.json")
    health_manifest, health_manifest_hash = _manifest(health_dir / "transformation_manifest.json")
    roads = _load_roads(processed, road_manifest)
    normal_points, swapped_points = _load_health(processed, health_manifest)
    source = osm_roads.geofabrik_thailand_source(raw)
    osm_roads._verify_source(source)
    boundary_summary = osm_roads._discover_boundary(source.artifact_path)
    osm_roads._require_boundary_contract(boundary_summary, osm_roads.PATTANI_BOUNDARY_CONTRACT)
    boundary = osm_roads._assemble_boundary(source.artifact_path)
    osm_roads._require_geometry_contract(boundary, osm_roads.PATTANI_BOUNDARY_CONTRACT)
    normal_inside = sum(boundary.intersects(item) for item in normal_points)
    swapped_inside = sum(boundary.intersects(item) for item in swapped_points)
    if normal_inside != len(normal_points) or swapped_inside != 0:
        raise ExposureAnalysisError("axis_policy_failed")
    road_counts = [0] * len(roads); healthcare_counts = [0] * len(normal_points)
    flood_count = 0
    for flood_batch in _iter_flood_pages(flood_dir, raw, flood_manifest):
        tree = STRtree(flood_batch)
        for index, road in enumerate(roads):
            road_counts[index] += _intersection_count(tree, flood_batch, road)
        for index, point in enumerate(normal_points):
            healthcare_counts[index] += _intersection_count(tree, flood_batch, point)
        flood_count += len(flood_batch)
    computation = ExposureComputation(tuple(road_counts), tuple(healthcare_counts), normal_inside, swapped_inside)

    try:
        destination.parent.mkdir(parents=True, exist_ok=True); destination.mkdir(exist_ok=False)
    except FileExistsError: raise ExposureAnalysisError("output_exists") from None
    except OSError: raise ExposureAnalysisError("publication_failed") from None

    published = False; temporary: Path | None = None
    descriptors: dict[str, dict[str, object]] = {}
    try:
        road_bytes = b"".join(_json_line({"analysis_sequence": index, "source_sequence": index,
            "intersects_flood": count > 0, "intersecting_polygon_count": count})
            for index, count in enumerate(computation.road_counts))
        health_bytes = b"".join(_json_line({"analysis_sequence": index, "source_sequence": index,
            "intersects_flood": count > 0, "intersecting_polygon_count": count})
            for index, count in enumerate(computation.healthcare_counts))
        road_path = destination / "road_exposure.jsonl"
        health_path = destination / "healthcare_exposure.jsonl"
        _publish_bytes(road_path, road_bytes); published = True
        _publish_bytes(health_path, health_bytes)
        descriptors[road_path.name] = _descriptor(road_path, len(computation.road_counts))
        descriptors[health_path.name] = _descriptor(health_path, len(computation.healthcare_counts))
        summary = _summary(flood_count, computation)
        summary_path = destination / "exposure_summary.json"
        _publish_bytes(summary_path, _json_bytes(summary) + b"\n")
        descriptors[summary_path.name] = _descriptor(summary_path, 1)
        manifest = {
            "schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION,
            "status": "complete", "analysis_id": ANALYSIS_ID,
            "completed_at_utc": timestamp,
            "inputs": {
                "flood": {"manifest_relative_path": (flood_dir / "transformation_manifest.json").relative_to(processed).as_posix(), "manifest_sha256": flood_manifest_hash, "data_sha256": _page_digest(flood_manifest)},
                "roads": {"manifest_relative_path": (road_dir / "extraction_manifest.json").relative_to(processed).as_posix(), "manifest_sha256": road_manifest_hash, "data_sha256": road_manifest["output"]["sha256"]},
                "healthcare": {"manifest_relative_path": (health_dir / "transformation_manifest.json").relative_to(processed).as_posix(), "manifest_sha256": health_manifest_hash, "data_sha256": health_manifest["output"]["sha256"]},
            },
            "outputs": descriptors,
            "ordering_policy": "source sequence ascending",
            "spatial_index": "page-bounded Shapely STRtree candidate query plus exact intersects",
            "resource_policy": {"maximum_flood_features_per_index": 1000,
                "maximum_total_flood_features": MAX_FLOOD_FEATURES,
                "minimum_available_memory_bytes": minimum_available_memory_bytes,
                "minimum_free_bytes": minimum_free_bytes},
            "crs_interpretation": {
                "osm": "officially documented OSM coordinate model",
                "gistda": "project RFC 7946 longitude-latitude interpretation; not explicit provider CRS",
                "dga": "normal axis uniquely consistent with validated Pattani boundary; observational only",
                "reprojected": False,
            },
            "limitations": {"exploratory_non_authoritative": True, "disruption_claimed": False,
                "severity_claimed": False, "accessibility_claimed": False,
                "completeness_claimed": False, "official_crs_claimed": False},
        }
        manifest_path = destination / "analysis_manifest.json"
        _publish_bytes(manifest_path, _json_bytes(manifest) + b"\n", completion=True)
        return ExposureAnalysisResult(destination, manifest_path, flood_count, len(roads),
            sum(value > 0 for value in computation.road_counts), len(normal_points),
            sum(value > 0 for value in computation.healthcare_counts),
            tuple((name, int(value["byte_count"]), str(value["sha256"])) for name, value in sorted(descriptors.items())))
    except ExposureAnalysisError: raise
    except Exception: raise ExposureAnalysisError("publication_failed", record_published=published) from None


def _iter_flood_pages(directory: Path, raw_root: Path, manifest: dict[str, object]) -> Iterable[list[MultiPolygon]]:
    if (manifest.get("status") != "complete" or manifest.get("transformation_schema_version") != "1.0"
            or manifest.get("feature_count") != 112073 or manifest.get("page_count") != 113):
        raise ExposureAnalysisError("schema_invalid")
    pages = manifest.get("pages")
    if not isinstance(pages, list) or len(pages) != 113: raise ExposureAnalysisError("schema_invalid")
    total = 0
    for page_index, page in enumerate(pages):
        if not isinstance(page, dict) or page.get("page_index") != page_index: raise ExposureAnalysisError("schema_invalid")
        source_path = raw_root / PurePosixPath(str(page.get("source_artifact_path")))
        _verify_file(source_path, str(page.get("source_stored_sha256")), None, raw_root)
        metadata_path = raw_root / PurePosixPath(str(page.get("source_metadata_path")))
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
            metadata_path.resolve(strict=True).relative_to(raw_root)
            if (metadata_path.is_symlink() or metadata.get("stored_artifact_sha256") != page.get("source_stored_sha256")
                    or metadata.get("relative_stored_artifact_path") != page.get("source_artifact_path")
                    or metadata.get("stored_artifact_byte_count") != source_path.stat().st_size):
                raise ValueError
        except Exception: raise ExposureAnalysisError("input_integrity_failed") from None
        try:
            source_object = json.loads(source_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicates)
        except Exception: raise ExposureAnalysisError("schema_invalid") from None
        if "crs" in source_object: raise ExposureAnalysisError("geometry_invalid")
        page_path = directory / PurePosixPath(str(page.get("output_page_path")))
        _verify_file(page_path, str(page.get("output_page_sha256")), page.get("output_page_byte_count"), directory)
        count = 0; geometries: list[MultiPolygon] = []
        with page_path.open("rb") as handle:
            for line in handle:
                try: record = json.loads(line, object_pairs_hook=_reject_duplicates)
                except Exception: raise ExposureAnalysisError("schema_invalid") from None
                if set(record) != {"feature_sequence", "feature"} or record["feature_sequence"] != count:
                    raise ExposureAnalysisError("schema_invalid")
                feature = record["feature"]
                if (not isinstance(feature, dict) or set(feature) != {"type", "id", "properties", "geometry"}
                        or feature.get("type") != "Feature" or not isinstance(feature.get("id"), str)
                        or not feature["id"] or not isinstance(feature.get("properties"), dict)):
                    raise ExposureAnalysisError("schema_invalid")
                geometry_data = feature.get("geometry")
                _validate_coordinates(geometry_data)
                try: geometry = shape(geometry_data)
                except Exception: raise ExposureAnalysisError("geometry_invalid") from None
                if not isinstance(geometry, MultiPolygon) or geometry.is_empty or not geometry.is_valid:
                    raise ExposureAnalysisError("geometry_invalid")
                geometries.append(geometry); count += 1; total += 1
                if total > MAX_FLOOD_FEATURES or len(geometries) > 1000:
                    raise ExposureAnalysisError("resource_limit")
        if count != page.get("output_record_count"): raise ExposureAnalysisError("schema_invalid")
        yield geometries
    if total != manifest["feature_count"]: raise ExposureAnalysisError("schema_invalid")


def _load_roads(root: Path, manifest: dict[str, object]) -> list[BaseGeometry]:
    output = manifest.get("output")
    if manifest.get("status") != "complete" or manifest.get("schema_version") != "1.0" or not isinstance(output, dict):
        raise ExposureAnalysisError("schema_invalid")
    path = root / PurePosixPath(str(output.get("relative_path")))
    _verify_file(path, str(output.get("sha256")), output.get("byte_count"), root)
    records: list[BaseGeometry] = []
    with path.open("rb") as handle:
        for line in handle:
            try: record = json.loads(line, object_pairs_hook=_reject_duplicates)
            except Exception: raise ExposureAnalysisError("schema_invalid") from None
            if (set(record) != {"feature_sequence", "way_sequence", "segment_sequence", "osm_way_id", "highway", "geometry"}
                    or record.get("feature_sequence") != len(records)):
                raise ExposureAnalysisError("schema_invalid")
            try: geometry = shape(record.get("geometry"))
            except Exception: raise ExposureAnalysisError("geometry_invalid") from None
            if (not isinstance(geometry, (LineString, MultiLineString)) or geometry.is_empty
                    or not geometry.is_valid or not _coordinates_in_range(geometry)):
                raise ExposureAnalysisError("geometry_invalid")
            records.append(geometry)
            if len(records) > MAX_ROAD_RECORDS: raise ExposureAnalysisError("resource_limit")
    if len(records) != output.get("segment_count"): raise ExposureAnalysisError("schema_invalid")
    return records


def _load_health(root: Path, manifest: dict[str, object]) -> tuple[list[Point], list[Point]]:
    output = manifest.get("output")
    if manifest.get("status") != "complete" or manifest.get("schema_version") != "1.0" or not isinstance(output, dict):
        raise ExposureAnalysisError("schema_invalid")
    path = root / PurePosixPath(str(output.get("relative_path")))
    _verify_file(path, str(output.get("sha256")), output.get("byte_count"), root)
    normal: list[Point] = []; swapped: list[Point] = []
    with path.open("rb") as handle:
        for line in handle:
            try: record = json.loads(line, object_pairs_hook=_reject_duplicates); fields = record["source_fields"]
            except Exception: raise ExposureAnalysisError("schema_invalid") from None
            if (set(record) != {"candidate_sequence", "source_fields"}
                    or record.get("candidate_sequence") != len(normal) or not isinstance(fields, dict)
                    or set(fields) != set(EXPECTED_FIELDS)):
                raise ExposureAnalysisError("schema_invalid")
            try: latitude = float(fields[LATITUDE_FIELD]); longitude = float(fields[LONGITUDE_FIELD])
            except Exception: raise ExposureAnalysisError("geometry_invalid") from None
            if not math.isfinite(latitude) or not math.isfinite(longitude): raise ExposureAnalysisError("geometry_invalid")
            normal.append(Point(longitude, latitude)); swapped.append(Point(latitude, longitude))
            if len(normal) > MAX_HEALTH_RECORDS: raise ExposureAnalysisError("resource_limit")
    if len(normal) != output.get("record_count"): raise ExposureAnalysisError("schema_invalid")
    return normal, swapped


def _intersection_count(tree: STRtree, geometries: Sequence[MultiPolygon], target: BaseGeometry) -> int:
    return sum(geometries[int(index)].intersects(target) for index in tree.query(target))


def _coordinates_in_range(geometry: BaseGeometry) -> bool:
    try:
        return all(math.isfinite(float(x)) and math.isfinite(float(y))
                   and -180 <= float(x) <= 180 and -90 <= float(y) <= 90
                   for x, y in get_coordinates(geometry))
    except Exception:
        return False


def _validate_coordinates(value: object) -> None:
    if not isinstance(value, dict) or value.get("type") != "MultiPolygon" or set(value) - {"type", "coordinates", "bbox"}:
        raise ExposureAnalysisError("geometry_invalid")
    def walk(item: object, depth: int = 0) -> None:
        if isinstance(item, list):
            if depth >= 3 and len(item) >= 2 and all(type(number) in {int, float} for number in item):
                lon, lat = item[0], item[1]
                if (not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90):
                    raise ExposureAnalysisError("geometry_invalid")
            else:
                for child in item: walk(child, depth + 1)
        else: raise ExposureAnalysisError("geometry_invalid")
    walk(value.get("coordinates"))


def _summary(flood_count: int, computation: ExposureComputation) -> dict[str, object]:
    road_exposed = sum(value > 0 for value in computation.road_counts)
    health_exposed = sum(value > 0 for value in computation.healthcare_counts)
    return {"policy_label": POLICY_VERSION, "input_flood_polygon_count": flood_count,
        "roads": {"total": len(computation.road_counts), "exposed": road_exposed,
                  "non_exposed": len(computation.road_counts) - road_exposed,
                  "intersection_count_histogram": _histogram(computation.road_counts)},
        "healthcare": {"total": len(computation.healthcare_counts), "exposed": health_exposed,
                  "non_exposed": len(computation.healthcare_counts) - health_exposed,
                  "intersection_count_histogram": _histogram(computation.healthcare_counts)},
        "dga_axis_consistency": {"normal_inside_pattani": computation.normal_axis_inside_count,
                                 "swapped_inside_pattani": computation.swapped_axis_inside_count},
        "rejected_or_invalid_input_count": 0,
        "limitations": ["geometric intersection only", "not a disruption or risk score",
                        "not an official CRS assertion", "no completeness or accessibility claim"]}


def _histogram(values: Iterable[int]) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values: result[str(value)] = result.get(str(value), 0) + 1
    return dict(sorted(result.items(), key=lambda pair: int(pair[0])))


def _manifest(path: Path) -> tuple[dict[str, object], str]:
    try:
        raw = path.read_bytes(); value = json.loads(raw, object_pairs_hook=_reject_duplicates)
        if not isinstance(value, dict): raise ValueError
        return value, hashlib.sha256(raw).hexdigest()
    except Exception: raise ExposureAnalysisError("input_integrity_failed") from None


def _verify_file(path: Path, digest: str, size: object, root: Path) -> None:
    try:
        path.resolve(strict=True).relative_to(root.resolve())
        if path.is_symlink() or not path.is_file(): raise OSError
        sha = hashlib.sha256(); total = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024): total += len(chunk); sha.update(chunk)
        if sha.hexdigest() != digest or (size is not None and total != size): raise ValueError
    except Exception: raise ExposureAnalysisError("input_integrity_failed") from None


def _descriptor(path: Path, records: int) -> dict[str, object]:
    raw = path.read_bytes()
    return {"relative_path": path.name, "byte_count": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "record_count": records}


def _page_digest(manifest: dict[str, object]) -> str:
    digest = hashlib.sha256()
    for page in manifest["pages"]: digest.update(str(page["output_page_sha256"]).encode("ascii"))
    return digest.hexdigest()


def _json_bytes(value: object) -> bytes:
    try: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except Exception: raise ExposureAnalysisError("schema_invalid") from None


def _json_line(value: object) -> bytes: return _json_bytes(value) + b"\n"


def _publish_bytes(path: Path, content: bytes, *, completion: bool = False) -> None:
    temporary: Path | None = None; linked = False
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name); handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.link(temporary, path); linked = True; temporary.unlink(); temporary = None
    except FileExistsError:
        cleanup_failed = False
        if temporary is not None:
            try: temporary.unlink(missing_ok=True)
            except OSError: cleanup_failed = True
        raise ExposureAnalysisError("cleanup_failed" if cleanup_failed else "output_exists",
            record_published=linked, completion_published=linked and completion,
            cleanup_failed=cleanup_failed) from None
    except OSError:
        cleanup_failed = False
        if temporary is not None:
            try: temporary.unlink(missing_ok=True)
            except OSError: cleanup_failed = True
        raise ExposureAnalysisError("cleanup_failed" if linked and cleanup_failed else "publication_failed",
            record_published=linked, completion_published=linked and completion,
            cleanup_failed=cleanup_failed) from None


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None: raise ExposureAnalysisError("invalid_input")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result: raise ValueError
        result[key] = value
    return result


def _contained(root: Path, path: Path) -> None:
    try: path.resolve(strict=False).relative_to(root)
    except Exception: raise ExposureAnalysisError("invalid_input") from None


def _existing_parent(path: Path) -> Path:
    while not path.exists() and path != path.parent: path = path.parent
    return path


def _available_memory() -> int:
    class Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong),
            ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
            ("total_page", ctypes.c_ulonglong), ("available_page", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong), ("available_virtual", ctypes.c_ulonglong),
            ("extended", ctypes.c_ulonglong)]
    status = Status(); status.length = ctypes.sizeof(status)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)): raise OSError
    except Exception: raise ExposureAnalysisError("resource_limit") from None
    return int(status.available)
