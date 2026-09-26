from __future__ import annotations

import math
from pathlib import Path
import socket

import pytest
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Point, Polygon

import src.analysis.flood_exposure as exposure


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr("dotenv.main.dotenv_values", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("configuration")))


def _floods() -> list[MultiPolygon]:
    return [MultiPolygon([Polygon([(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)])]),
            MultiPolygon([Polygon([(1, 1), (3, 1), (3, 3), (1, 3), (1, 1)])])]


def test_inside_outside_touching_multipart_and_multiple_intersections() -> None:
    boundary = MultiPolygon([Polygon([(-1, -1), (4, -1), (4, 4), (-1, 4), (-1, -1)])])
    roads = [LineString([(-1, 1), (4, 1)]), LineString([(5, 5), (6, 6)]),
             MultiLineString([[(-1, 0), (0, 0)], [(2, 2), (4, 4)]])]
    normal = [Point(1.5, 1.5), Point(3.5, 3.5)]
    swapped = [Point(10, 10), Point(11, 11)]
    result = exposure.compute_exposure(_floods(), roads, normal, swapped, boundary)
    assert result.road_counts == (2, 0, 2)
    assert result.healthcare_counts == (2, 0)
    assert result.normal_axis_inside_count == 2
    assert result.swapped_axis_inside_count == 0


def test_spatial_index_reconciles_with_direct_intersection() -> None:
    floods = _floods(); targets = [LineString([(x, -1), (x, 4)]) for x in range(5)]
    boundary = MultiPolygon([Polygon([(-2, -2), (6, -2), (6, 6), (-2, 6), (-2, -2)])])
    result = exposure.compute_exposure(floods, targets, [], [], boundary)
    assert result.road_counts == tuple(sum(flood.intersects(target) for flood in floods) for target in targets)


@pytest.mark.parametrize("coordinates", [
    [[[[math.inf, 1], [1, 1], [1, 2], [math.inf, 1]]]],
    [[[[181, 1], [1, 1], [1, 2], [181, 1]]]],
    [[[[1, 91], [1, 1], [1, 2], [1, 91]]]],
])
def test_invalid_nonfinite_and_out_of_range_coordinates(coordinates: object) -> None:
    with pytest.raises(exposure.ExposureAnalysisError) as raised:
        exposure._validate_coordinates({"type": "MultiPolygon", "coordinates": coordinates})
    assert raised.value.category == "geometry_invalid"
    assert "coordinates" not in repr(raised.value)


def test_conflicting_crs_policy_is_fixed_category() -> None:
    with pytest.raises(exposure.ExposureAnalysisError) as raised:
        exposure._validate_coordinates({"type": "MultiPolygon", "coordinates": [], "crs": {}})
    assert raised.value.category == "geometry_invalid"


def test_publication_collision_and_failure_leave_existing_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    destination = tmp_path / "result"; destination.write_bytes(b"original")
    with pytest.raises(exposure.ExposureAnalysisError) as raised:
        exposure._publish_bytes(destination, b"new")
    assert raised.value.category == "output_exists"
    assert destination.read_bytes() == b"original"
    monkeypatch.setattr(exposure.os, "link", lambda *args: (_ for _ in ()).throw(OSError("secret")))
    with pytest.raises(exposure.ExposureAnalysisError) as failed:
        exposure._publish_bytes(tmp_path / "new", b"value")
    assert failed.value.category == "publication_failed"
    assert "secret" not in repr(failed.value)
    assert not list(tmp_path.glob("*.tmp"))


def test_completion_cleanup_failure_is_truthful(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.unlink
    def fail_temporary(path: Path, *args: object, **kwargs: object) -> None:
        if path.name.endswith(".tmp"):
            raise OSError("private")
        original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_temporary)
    destination = tmp_path / "analysis_manifest.json"
    with pytest.raises(exposure.ExposureAnalysisError) as raised:
        exposure._publish_bytes(destination, b"{}\n", completion=True)
    assert raised.value.category == "cleanup_failed"
    assert raised.value.record_published and raised.value.completion_published
    assert raised.value.cleanup_failed and destination.read_bytes() == b"{}\n"
    assert "private" not in repr(raised.value)
