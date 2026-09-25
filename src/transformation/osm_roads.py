"""Deterministic offline extraction of observed Pattani OSM highway ways."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import unicodedata
from typing import Callable, Iterable

import osmium
from shapely import wkb
from shapely.geometry import LineString, MultiLineString, Polygon, MultiPolygon


ROAD_SCHEMA_VERSION = "1.0"
ROAD_POLICY_VERSION = "pattani-osm-highways-observed-v1"
DEFAULT_TRANSFORMATION_ID = "pattani-osm-highways-v1-20260925-01"
SOURCE_SHA256 = "fc4117130af85c248c24376ba81bc593698d415f70907a994cd6813793e44b13"
SOURCE_MD5 = "4558c600b0e70e355c4436bd3ca80ac9"
SOURCE_BYTES = 327_676_785
SOURCE_URL = "https://download.geofabrik.de/asia/thailand-260923.osm.pbf"
MAX_SOURCE_BYTES = 419_430_400
MIN_AVAILABLE_MEMORY_BYTES = 512 * 1024 * 1024
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024
MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024
MAX_OUTPUT_RECORDS = 10_000_000

_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MD5 = re.compile(r"[0-9a-f]{32}\Z")
_TARGET_NAMES = frozenset({
    unicodedata.normalize("NFC", "ปัตตานี"),
    unicodedata.normalize("NFC", "Pattani"),
})
_SAFE_ERRORS = frozenset({
    "boundary_assembly_failed",
    "boundary_contract_changed",
    "boundary_not_unique",
    "cleanup_failed",
    "insufficient_free_space",
    "insufficient_memory",
    "invalid_input",
    "output_already_exists",
    "output_limit_exceeded",
    "publication_failed",
    "road_extraction_failed",
    "serialization_failed",
    "source_integrity_failed",
})


class RoadExtractionError(RuntimeError):
    """Fixed-category error with truthful publication state."""

    def __init__(
        self,
        category: str,
        *,
        data_published: bool = False,
        manifest_published: bool = False,
        cleanup_failed: bool = False,
    ) -> None:
        self.category = category if category in _SAFE_ERRORS else "invalid_input"
        self.data_published = data_published is True
        self.manifest_published = manifest_published is True
        self.cleanup_failed = cleanup_failed is True
        super().__init__(self.category)

    def __repr__(self) -> str:
        return (
            "RoadExtractionError("
            f"category={self.category!r}, data_published={self.data_published!r}, "
            f"manifest_published={self.manifest_published!r}, "
            f"cleanup_failed={self.cleanup_failed!r})"
        )


@dataclass(frozen=True, repr=False)
class RoadSource:
    artifact_path: Path
    metadata_path: Path
    relative_artifact_path: str
    expected_bytes: int
    expected_md5: str
    expected_sha256: str
    approved_url: str

    def __repr__(self) -> str:
        return "RoadSource()"


@dataclass(frozen=True)
class BoundaryContract:
    admin_level: str
    relation_type: str
    member_count: int
    outer_member_count: int
    inner_member_count: int
    way_member_count: int
    node_member_count: int
    polygon_count: int
    exterior_ring_count: int
    interior_ring_count: int


PATTANI_BOUNDARY_CONTRACT = BoundaryContract(
    admin_level="4",
    relation_type="boundary",
    member_count=23,
    outer_member_count=21,
    inner_member_count=0,
    way_member_count=21,
    node_member_count=2,
    polygon_count=1,
    exterior_ring_count=1,
    interior_ring_count=0,
)


@dataclass(frozen=True)
class RoadExtractionResult:
    transformation_id: str
    directory: Path
    data_path: Path
    manifest_path: Path
    way_count: int
    segment_count: int
    byte_count: int
    sha256: str
    highway_category_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class _BoundarySummary:
    admin_level: str
    relation_type: str
    member_count: int
    outer_member_count: int
    inner_member_count: int
    way_member_count: int
    node_member_count: int


def geofabrik_thailand_source(raw_root: Path) -> RoadSource:
    root = Path(raw_root).resolve()
    stem = f"resource-thailand-260923.osm.pbf__sha256-{SOURCE_SHA256}"
    relative = (
        "infrastructure/roads/osm/geofabrik/"
        f"{stem}.osm.pbf"
    )
    return RoadSource(
        artifact_path=root / PurePosixPath(relative),
        metadata_path=(
            root
            / "infrastructure/roads/osm/geofabrik"
            / f"{stem}.metadata.json"
        ),
        relative_artifact_path=relative,
        expected_bytes=SOURCE_BYTES,
        expected_md5=SOURCE_MD5,
        expected_sha256=SOURCE_SHA256,
        approved_url=SOURCE_URL,
    )


def extract_pattani_osm_roads(
    source: RoadSource,
    *,
    output_root: Path,
    transformation_id: str = DEFAULT_TRANSFORMATION_ID,
    boundary_contract: BoundaryContract = PATTANI_BOUNDARY_CONTRACT,
    minimum_free_bytes: int = MIN_FREE_BYTES,
    minimum_available_memory_bytes: int = MIN_AVAILABLE_MEMORY_BYTES,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    max_output_records: int = MAX_OUTPUT_RECORDS,
    memory_probe: Callable[[], int] | None = None,
) -> RoadExtractionResult:
    """Verify, select, clip, and immutably publish one offline extraction."""

    _validate_inputs(
        source,
        transformation_id,
        boundary_contract,
        minimum_free_bytes,
        minimum_available_memory_bytes,
        max_output_bytes,
        max_output_records,
    )
    _verify_source(source)
    root = Path(output_root).resolve()
    destination = (
        root / "infrastructure/roads/osm/pattani" / transformation_id
    )
    _require_contained(root, destination)
    if destination.exists() or destination.is_symlink():
        raise RoadExtractionError("output_already_exists")
    try:
        free_bytes = shutil.disk_usage(_existing_parent(root)).free
    except OSError:
        raise RoadExtractionError("invalid_input") from None
    if free_bytes < minimum_free_bytes:
        raise RoadExtractionError("insufficient_free_space")
    available_memory = (memory_probe or _available_memory_bytes)()
    if available_memory < minimum_available_memory_bytes:
        raise RoadExtractionError("insufficient_memory")

    summary = _discover_boundary(source.artifact_path)
    _require_boundary_contract(summary, boundary_contract)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(exist_ok=False)
    except FileExistsError:
        raise RoadExtractionError("output_already_exists") from None
    except OSError:
        raise RoadExtractionError("publication_failed") from None

    boundary = _assemble_boundary(source.artifact_path)
    _require_geometry_contract(boundary, boundary_contract)

    temporary: Path | None = None
    data_path: Path | None = None
    data_published = False
    manifest_published = False
    try:
        temporary, extraction = _write_extraction_temporary(
            source.artifact_path,
            boundary,
            destination,
            max_output_bytes=max_output_bytes,
            max_output_records=max_output_records,
        )
        data_name = f"pattani_osm_highways__sha256-{extraction['sha256']}.jsonl"
        data_path = destination / data_name
        _link_no_replace(temporary, data_path)
        data_published = True
        _cleanup_one(temporary, data_published=True)
        temporary = None

        manifest = {
            "schema_version": ROAD_SCHEMA_VERSION,
            "policy_version": ROAD_POLICY_VERSION,
            "status": "complete",
            "transformation_id": transformation_id,
            "source": {
                "relative_artifact_path": source.relative_artifact_path,
                "byte_count": source.expected_bytes,
                "md5": source.expected_md5,
                "sha256": source.expected_sha256,
                "approved_url": source.approved_url,
            },
            "boundary": {
                "selection_basis": "observed_exact_name_and_administrative_tags",
                "admin_level": boundary_contract.admin_level,
                "relation_type": boundary_contract.relation_type,
                "geometry_type": boundary.geom_type,
                "polygon_count": boundary_contract.polygon_count,
                "exterior_ring_count": boundary_contract.exterior_ring_count,
                "interior_ring_count": boundary_contract.interior_ring_count,
                "valid": True,
                "repaired": False,
                "simplified": False,
            },
            "tools": {
                "osmium": _package_version("osmium"),
                "shapely": _package_version("shapely"),
            },
            "output": {
                "relative_path": data_path.relative_to(root).as_posix(),
                "byte_count": extraction["byte_count"],
                "sha256": extraction["sha256"],
                "way_count": extraction["way_count"],
                "segment_count": extraction["segment_count"],
                "highway_category_counts": extraction["category_counts"],
            },
            "interpretation": {
                "highway_values_are_observed_categories_only": True,
                "drivability_inferred": False,
                "crs_inferred": False,
                "completeness_claimed": False,
            },
            "resource_policy": {
                "max_source_bytes": MAX_SOURCE_BYTES,
                "minimum_available_memory_bytes": minimum_available_memory_bytes,
                "minimum_free_bytes": minimum_free_bytes,
                "max_output_bytes": max_output_bytes,
                "max_output_records": max_output_records,
            },
        }
        manifest_bytes = _json_bytes(manifest) + b"\n"
        manifest_path = destination / "extraction_manifest.json"
        manifest_temp = _write_temporary(destination, ".manifest.", manifest_bytes)
        temporary = manifest_temp
        _link_no_replace(manifest_temp, manifest_path)
        manifest_published = True
        _cleanup_one(manifest_temp, data_published=True, manifest_published=True)
        temporary = None
        categories = tuple(sorted(extraction["category_counts"].items()))
        return RoadExtractionResult(
            transformation_id=transformation_id,
            directory=destination,
            data_path=data_path,
            manifest_path=manifest_path,
            way_count=extraction["way_count"],
            segment_count=extraction["segment_count"],
            byte_count=extraction["byte_count"],
            sha256=extraction["sha256"],
            highway_category_counts=categories,
        )
    except RoadExtractionError as error:
        cleanup_failed = not _cleanup_paths(temporary)
        raise RoadExtractionError(
            error.category,
            data_published=data_published or error.data_published,
            manifest_published=manifest_published or error.manifest_published,
            cleanup_failed=cleanup_failed or error.cleanup_failed,
        ) from None
    except Exception:
        cleanup_failed = not _cleanup_paths(temporary)
        raise RoadExtractionError(
            "road_extraction_failed",
            data_published=data_published,
            manifest_published=manifest_published,
            cleanup_failed=cleanup_failed,
        ) from None


def _is_boundary(tags: object) -> bool:
    try:
        if tags.get("boundary") != "administrative":  # type: ignore[attr-defined]
            return False
        return any(
            (tag.k == "name" or tag.k.startswith("name:"))
            and unicodedata.normalize("NFC", tag.v) in _TARGET_NAMES
            for tag in tags  # type: ignore[union-attr]
        )
    except Exception:
        return False


def _discover_boundary(path: Path) -> _BoundarySummary:
    class Handler(osmium.SimpleHandler):
        def __init__(self) -> None:
            super().__init__()
            self.items: list[_BoundarySummary] = []

        def relation(self, relation: object) -> None:
            if not _is_boundary(relation.tags):  # type: ignore[attr-defined]
                return
            total = outer = inner = ways = nodes = 0
            for member in relation.members:  # type: ignore[attr-defined]
                total += 1
                outer += member.role == "outer"
                inner += member.role == "inner"
                kind = _member_kind(member.type)
                ways += kind == "way"
                nodes += kind == "node"
            self.items.append(_BoundarySummary(
                admin_level=relation.tags.get("admin_level"),  # type: ignore[attr-defined]
                relation_type=relation.tags.get("type"),  # type: ignore[attr-defined]
                member_count=total,
                outer_member_count=outer,
                inner_member_count=inner,
                way_member_count=ways,
                node_member_count=nodes,
            ))

    handler = Handler()
    try:
        handler.apply_file(str(path), locations=False)
    except Exception:
        raise RoadExtractionError("boundary_assembly_failed") from None
    if len(handler.items) != 1:
        raise RoadExtractionError("boundary_not_unique")
    return handler.items[0]


def _assemble_boundary(path: Path) -> MultiPolygon:
    class Handler(osmium.SimpleHandler):
        def __init__(self) -> None:
            super().__init__()
            self.factory = osmium.geom.WKBFactory()
            self.items: list[MultiPolygon] = []
            self.failed = False

        def area(self, area: object) -> None:
            if area.from_way() or not _is_boundary(area.tags):  # type: ignore[attr-defined]
                return
            try:
                geometry = wkb.loads(
                    self.factory.create_multipolygon(area),  # type: ignore[arg-type]
                    hex=True,
                )
                self.items.append(geometry)
            except Exception:
                self.failed = True

    handler = Handler()
    try:
        handler.apply_file(str(path), locations=True, idx="flex_mem")
    except Exception:
        raise RoadExtractionError("boundary_assembly_failed") from None
    if handler.failed or len(handler.items) != 1:
        raise RoadExtractionError("boundary_assembly_failed")
    geometry = handler.items[0]
    if not isinstance(geometry, MultiPolygon):
        raise RoadExtractionError("boundary_contract_changed")
    return geometry


def _write_extraction_temporary(
    source_path: Path,
    boundary: MultiPolygon,
    destination: Path,
    *,
    max_output_bytes: int,
    max_output_records: int,
) -> tuple[Path, dict[str, object]]:
    handle = None
    temporary: Path | None = None
    digest = hashlib.sha256()
    byte_count = 0
    way_count = 0
    segment_count = 0
    categories: dict[str, int] = {}

    class Handler(osmium.SimpleHandler):
        def way(self, way: object) -> None:
            nonlocal byte_count, way_count, segment_count
            highway = way.tags.get("highway")  # type: ignore[attr-defined]
            if not isinstance(highway, str) or not highway:
                return
            try:
                coordinates = [
                    (node.lon, node.lat) for node in way.nodes  # type: ignore[attr-defined]
                ]
                if len(coordinates) < 2:
                    return
                clipped = boundary.intersection(LineString(coordinates))
                segments = _line_segments(clipped)
            except Exception:
                raise RoadExtractionError("road_extraction_failed") from None
            if not segments:
                return
            current_way_sequence = way_count
            way_count += 1
            categories[highway] = categories.get(highway, 0) + 1
            for index, segment in enumerate(segments):
                if segment_count >= max_output_records:
                    raise RoadExtractionError("output_limit_exceeded")
                record = {
                    "feature_sequence": segment_count,
                    "way_sequence": current_way_sequence,
                    "segment_sequence": index,
                    "osm_way_id": int(way.id),  # type: ignore[attr-defined]
                    "highway": highway,
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [list(pair) for pair in segment.coords],
                    },
                }
                line = _json_bytes(record) + b"\n"
                if byte_count + len(line) > max_output_bytes:
                    raise RoadExtractionError("output_limit_exceeded")
                try:
                    handle.write(line)
                except OSError:
                    raise RoadExtractionError("publication_failed") from None
                digest.update(line)
                byte_count += len(line)
                segment_count += 1

    try:
        handle = tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination,
            prefix=".roads.",
            suffix=".tmp",
            delete=False,
        )
        temporary = Path(handle.name)
        road_handler = Handler()
        road_handler.apply_file(str(source_path), locations=True, idx="flex_mem")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
    except RoadExtractionError:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if not _cleanup_paths(temporary):
            raise RoadExtractionError("cleanup_failed", cleanup_failed=True) from None
        raise
    except Exception:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        cleanup_failed = not _cleanup_paths(temporary)
        raise RoadExtractionError(
            "road_extraction_failed", cleanup_failed=cleanup_failed
        ) from None
    if temporary is None:
        raise RoadExtractionError("publication_failed")
    return temporary, {
        "way_count": way_count,
        "segment_count": segment_count,
        "byte_count": byte_count,
        "sha256": digest.hexdigest(),
        "category_counts": dict(sorted(categories.items())),
    }


def _line_segments(geometry: object) -> list[LineString]:
    if isinstance(geometry, LineString):
        candidates: Iterable[LineString] = (geometry,)
    elif isinstance(geometry, MultiLineString):
        candidates = geometry.geoms
    elif hasattr(geometry, "geoms"):
        candidates = (
            item for item in geometry.geoms if isinstance(item, LineString)
        )
    else:
        candidates = ()
    segments = [item for item in candidates if not item.is_empty and item.length > 0]
    return sorted(segments, key=lambda item: _json_bytes(list(item.coords)))


def _require_boundary_contract(
    observed: _BoundarySummary, expected: BoundaryContract
) -> None:
    if (
        observed.admin_level != expected.admin_level
        or observed.relation_type != expected.relation_type
        or observed.member_count != expected.member_count
        or observed.outer_member_count != expected.outer_member_count
        or observed.inner_member_count != expected.inner_member_count
        or observed.way_member_count != expected.way_member_count
        or observed.node_member_count != expected.node_member_count
    ):
        raise RoadExtractionError("boundary_contract_changed")


def _require_geometry_contract(
    geometry: MultiPolygon, expected: BoundaryContract
) -> None:
    polygons = list(geometry.geoms)
    exterior_count = len(polygons)
    interior_count = sum(len(polygon.interiors) for polygon in polygons)
    rings = [polygon.exterior for polygon in polygons]
    rings.extend(ring for polygon in polygons for ring in polygon.interiors)
    coordinates_finite = all(
        math.isfinite(value)
        for polygon in polygons
        for ring in (polygon.exterior, *polygon.interiors)
        for coordinate in ring.coords
        for value in coordinate
    )
    if (
        geometry.is_empty
        or not geometry.is_valid
        or not coordinates_finite
        or not rings
        or not all(ring.is_closed for ring in rings)
        or len(polygons) != expected.polygon_count
        or exterior_count != expected.exterior_ring_count
        or interior_count != expected.interior_ring_count
    ):
        raise RoadExtractionError("boundary_contract_changed")


def _verify_source(source: RoadSource) -> None:
    try:
        if (
            source.expected_bytes <= 0
            or source.expected_bytes > MAX_SOURCE_BYTES
            or not _MD5.fullmatch(source.expected_md5)
            or not _SHA256.fullmatch(source.expected_sha256)
            or not _safe_relative(source.relative_artifact_path)
            or not source.artifact_path.is_file()
            or not source.metadata_path.is_file()
            or source.artifact_path.is_symlink()
            or source.metadata_path.is_symlink()
        ):
            raise RoadExtractionError("source_integrity_failed")
        sha256 = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        total = 0
        with source.artifact_path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                total += len(chunk)
                sha256.update(chunk)
                md5.update(chunk)
        metadata = json.loads(
            source.metadata_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicates,
        )
        if (
            total != source.expected_bytes
            or sha256.hexdigest() != source.expected_sha256
            or md5.hexdigest() != source.expected_md5
            or metadata.get("byte_count") != total
            or metadata.get("sha256") != source.expected_sha256
            or metadata.get("provider_md5") != source.expected_md5
            or metadata.get("relative_artifact_path") != source.relative_artifact_path
            or metadata.get("approved_download_url") != source.approved_url
        ):
            raise RoadExtractionError("source_integrity_failed")
    except RoadExtractionError:
        raise
    except Exception:
        raise RoadExtractionError("source_integrity_failed") from None


def _validate_inputs(
    source: object,
    transformation_id: object,
    boundary_contract: object,
    *limits: object,
) -> None:
    if (
        not isinstance(source, RoadSource)
        or not isinstance(boundary_contract, BoundaryContract)
        or not isinstance(transformation_id, str)
        or not _SAFE_IDENTIFIER.fullmatch(transformation_id)
        or any(type(value) is not int or value <= 0 for value in limits)
    ):
        raise RoadExtractionError("invalid_input")


def _member_kind(value: object) -> str:
    name = getattr(value, "name", None)
    raw = getattr(value, "value", None)
    if name in {"node", "way", "relation"}:
        return name
    if raw in {"n", "w", "r"}:
        return {"n": "node", "w": "way", "r": "relation"}[raw]
    text = str(value).casefold()
    if text == "n" or text.endswith("node"):
        return "node"
    if text == "w" or text.endswith("way"):
        return "way"
    if text == "r" or text.endswith("relation"):
        return "relation"
    return "unknown"


def _package_version(name: str) -> str:
    from importlib.metadata import version

    return version(name)


def _available_memory_bytes() -> int:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(status)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError
    except (AttributeError, OSError):
        raise RoadExtractionError("invalid_input") from None
    return int(status.available_physical)


def _json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise RoadExtractionError("serialization_failed") from None


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _safe_relative(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(
        part not in {"", ".", ".."} for part in path.parts
    )


def _existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def _require_contained(root: Path, destination: Path) -> None:
    try:
        destination.resolve(strict=False).relative_to(root)
        current = root
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        for part in destination.relative_to(root).parts:
            current = current / part
            if current.exists() or current.is_symlink():
                details = current.lstat()
                if stat.S_ISLNK(details.st_mode) or bool(
                    getattr(details, "st_file_attributes", 0) & reparse
                ):
                    raise RoadExtractionError("invalid_input")
    except RoadExtractionError:
        raise
    except Exception:
        raise RoadExtractionError("invalid_input") from None


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
        raise RoadExtractionError(
            "publication_failed", cleanup_failed=cleanup_failed
        ) from None


def _link_no_replace(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except FileExistsError:
        raise RoadExtractionError("output_already_exists") from None
    except OSError:
        raise RoadExtractionError("publication_failed") from None


def _cleanup_one(
    path: Path,
    *,
    data_published: bool,
    manifest_published: bool = False,
) -> None:
    if not _cleanup_paths(path):
        raise RoadExtractionError(
            "cleanup_failed",
            data_published=data_published,
            manifest_published=manifest_published,
            cleanup_failed=True,
        )


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
