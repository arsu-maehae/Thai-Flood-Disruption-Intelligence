from __future__ import annotations

import csv
import io
from pathlib import Path
import socket

import pytest
from shapely.geometry import LineString, MultiPolygon, Point, Polygon

import src.analysis.temporal_exposure as temporal


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr("dotenv.main.dotenv_values", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("configuration")))


def _polygon(x1: float, x2: float) -> MultiPolygon:
    return MultiPolygon([Polygon([(x1, 0), (x2, 0), (x2, 2), (x1, 2), (x1, 0)])])


def test_binary_years_frequency_and_overlap_deduplication() -> None:
    flags = [(1, 0, *([0] * 12)), (1, 1, *([0] * 12)), (0, 1, *([0] * 12))]
    result = temporal.compute_temporal_exposure(
        [_polygon(0, 2), _polygon(1, 3), _polygon(4, 5)], flags, [1, 3, 1],
        [LineString([(-1, 1), (3, 1)]), LineString([(4, 1), (5, 1)])],
        [Point(1.5, 1), Point(4.5, 1)],
    )
    assert result.road_masks == (3, 2)
    assert result.healthcare_masks == (3, 2)
    assert result.frequency.match_count == 2
    assert result.frequency.mismatch_count == 1
    assert result.frequency.missing_or_invalid_count == 0
    assert result.frequency.per_year_active_counts[:2] == (2, 2)


@pytest.mark.parametrize("bad", [True, -1, 2, "1", None])
def test_year_flags_reject_nonbinary_and_booleans(bad: object) -> None:
    flags = [bad, *([0] * 13)]
    with pytest.raises(temporal.TemporalExposureError) as raised:
        temporal.compute_temporal_exposure([_polygon(0, 1)], [flags], [0], [], [])
    assert raised.value.category == "yearly_field_invalid"
    assert str(bad) not in repr(raised.value)


def test_road_category_and_healthcare_csv_are_stable_and_reconciled() -> None:
    categories = ["secondary", "primary", "primary"]
    masks = [2, 1, 3]
    first = temporal._road_category_csv(categories, masks)
    assert first == temporal._road_category_csv(categories, masks)
    rows = list(csv.reader(io.StringIO(first.decode())))
    assert [row[0] for row in rows[1:]] == ["primary", "secondary"]
    assert sum(int(row[-1]) for row in rows[1:]) == 3
    health = list(csv.reader(io.StringIO(temporal._health_csv([0, 1, 3]).decode())))
    assert health[1][-2:] == ["2", "3"]


def test_publication_collision_is_incomplete_and_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "result"; path.write_bytes(b"old")
    with pytest.raises(temporal.TemporalExposureError) as raised:
        temporal._publish(path, b"new")
    assert raised.value.category == "output_exists" and path.read_bytes() == b"old"
    monkeypatch.setattr(temporal.os, "link", lambda *args: (_ for _ in ()).throw(OSError("private")))
    with pytest.raises(temporal.TemporalExposureError) as failed:
        temporal._publish(tmp_path / "new", b"value")
    assert failed.value.category == "publication_failed"
    assert "private" not in repr(failed.value)
    assert not list(tmp_path.glob("*.tmp"))


def test_published_completion_cleanup_fault_is_truthful(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.unlink
    calls = 0
    def fail_once(path: Path, *args: object, **kwargs: object) -> None:
        nonlocal calls
        if path.name.endswith(".tmp") and calls == 0:
            calls += 1
            raise OSError("private")
        original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_once)
    destination = tmp_path / "analysis_manifest.json"
    with pytest.raises(temporal.TemporalExposureError) as raised:
        temporal._publish(destination, b"{}\n", completion=True)
    assert raised.value.category == "cleanup_failed"
    assert raised.value.record_published and raised.value.completion_published
    assert not raised.value.cleanup_failed
    assert destination.read_bytes() == b"{}\n" and not list(tmp_path.glob("*.tmp"))
    assert "private" not in repr(raised.value)
