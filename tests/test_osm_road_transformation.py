from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import socket

import pytest

import src.transformation.osm_roads as roads
from src.transformation.osm_roads import (
    BoundaryContract,
    RoadExtractionError,
    RoadSource,
    extract_pattani_osm_roads,
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("external access is forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr("dotenv.main.dotenv_values", forbidden)


def synthetic_osm() -> bytes:
    nodes = {
        1: (0, 0), 2: (4, 0), 3: (4, 4), 4: (3, 4),
        5: (3, 1), 6: (1, 1), 7: (1, 4), 8: (0, 4),
        100: (-1, 0.5), 101: (5, 0.5),
        102: (-1, 5), 103: (5, 5),
        104: (-1, 2), 105: (5, 2),
    }
    node_xml = "".join(
        f'<node id="{identifier}" lon="{lon}" lat="{lat}" version="1"/>'
        for identifier, (lon, lat) in nodes.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<osm version="0.6" generator="synthetic">'
        f'{node_xml}'
        '<way id="10" version="1">'
        '<nd ref="1"/><nd ref="2"/><nd ref="3"/><nd ref="4"/>'
        '<nd ref="5"/><nd ref="6"/><nd ref="7"/><nd ref="8"/>'
        '<nd ref="1"/></way>'
        '<way id="20" version="1"><nd ref="100"/><nd ref="101"/>'
        '<tag k="highway" v="primary"/></way>'
        '<way id="21" version="1"><nd ref="102"/><nd ref="103"/>'
        '<tag k="highway" v="service"/></way>'
        '<way id="22" version="1"><nd ref="104"/><nd ref="105"/>'
        '<tag k="highway" v="secondary"/></way>'
        '<relation id="30" version="1"><member type="way" ref="10" role="outer"/>'
        '<tag k="type" v="boundary"/><tag k="boundary" v="administrative"/>'
        '<tag k="admin_level" v="4"/><tag k="name:en" v="Pattani"/>'
        '</relation></osm>'
    ).encode("utf-8")


def source_fixture(tmp_path: Path) -> tuple[RoadSource, BoundaryContract]:
    tmp_path.mkdir(parents=True)
    artifact = tmp_path / "source.osm"
    content = synthetic_osm()
    artifact.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
    metadata = tmp_path / "source.metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "approved_download_url": "https://example.test/source.osm",
                "byte_count": len(content),
                "provider_md5": md5,
                "relative_artifact_path": "source.osm",
                "sha256": digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    source = RoadSource(
        artifact_path=artifact,
        metadata_path=metadata,
        relative_artifact_path="source.osm",
        expected_bytes=len(content),
        expected_md5=md5,
        expected_sha256=digest,
        approved_url="https://example.test/source.osm",
    )
    contract = BoundaryContract(
        admin_level="4",
        relation_type="boundary",
        member_count=1,
        outer_member_count=1,
        inner_member_count=0,
        way_member_count=1,
        node_member_count=0,
        polygon_count=1,
        exterior_ring_count=1,
        interior_ring_count=0,
    )
    return source, contract


def run_synthetic(
    source: RoadSource,
    contract: BoundaryContract,
    output: Path,
    transformation_id: str,
):
    return extract_pattani_osm_roads(
        source,
        output_root=output,
        transformation_id=transformation_id,
        boundary_contract=contract,
        minimum_free_bytes=1,
        minimum_available_memory_bytes=1,
        max_output_bytes=1_000_000,
        max_output_records=100,
        memory_probe=lambda: 1_000_000,
    )


def test_crossing_outside_and_multipart_ways_are_deterministic(tmp_path: Path) -> None:
    source, contract = source_fixture(tmp_path / "input")
    first = run_synthetic(source, contract, tmp_path / "one", "synthetic-v1")
    second = run_synthetic(source, contract, tmp_path / "two", "synthetic-v2")

    first_bytes = first.data_path.read_bytes()
    assert first_bytes == second.data_path.read_bytes()
    assert first.sha256 == hashlib.sha256(first_bytes).hexdigest()
    assert first.byte_count == len(first_bytes)
    assert first.way_count == 2
    assert first.segment_count == 3
    assert dict(first.highway_category_counts) == {"primary": 1, "secondary": 1}
    records = [json.loads(line) for line in first_bytes.splitlines()]
    assert len(records) == 3
    assert all(set(record) == {
        "feature_sequence", "geometry", "highway", "osm_way_id",
        "segment_sequence", "way_sequence",
    } for record in records)
    assert all(record["geometry"]["type"] == "LineString" for record in records)
    assert "service" not in {record["highway"] for record in records}

    manifest = json.loads(first.manifest_path.read_bytes())
    assert manifest["status"] == "complete"
    assert manifest["output"]["sha256"] == first.sha256
    assert manifest["output"]["byte_count"] == first.byte_count
    assert manifest["output"]["segment_count"] == 3
    assert manifest["boundary"]["repaired"] is False
    assert manifest["boundary"]["simplified"] is False
    assert not list(first.directory.glob("*.tmp"))


def test_boundary_drift_is_rejected_before_output_creation(tmp_path: Path) -> None:
    source, contract = source_fixture(tmp_path / "input")
    drifted = replace(contract, admin_level="5")
    with pytest.raises(RoadExtractionError, match="^boundary_contract_changed$"):
        run_synthetic(source, drifted, tmp_path / "output", "drift-v1")
    assert not (tmp_path / "output").exists()


def test_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    source, contract = source_fixture(tmp_path / "input")
    output = tmp_path / "output"
    result = run_synthetic(source, contract, output, "collision-v1")
    before = result.manifest_path.read_bytes()
    with pytest.raises(RoadExtractionError, match="^output_already_exists$"):
        run_synthetic(source, contract, output, "collision-v1")
    assert result.manifest_path.read_bytes() == before


@pytest.mark.parametrize("fail_link_number", [1, 2])
def test_failed_publication_never_appears_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_link_number: int,
) -> None:
    source, contract = source_fixture(tmp_path / "input")
    real_link = os.link
    calls = 0

    def fail_selected(source_path: object, destination_path: object) -> None:
        nonlocal calls
        calls += 1
        if calls == fail_link_number:
            raise OSError("private publication detail")
        real_link(source_path, destination_path)

    monkeypatch.setattr(roads.os, "link", fail_selected)
    with pytest.raises(RoadExtractionError, match="^publication_failed$") as caught:
        run_synthetic(source, contract, tmp_path / "output", f"failure-{fail_link_number}")

    error = caught.value
    assert error.data_published is (fail_link_number == 2)
    assert error.manifest_published is False
    assert "private" not in str(error) + repr(error)
    destination = (
        tmp_path / "output/infrastructure/roads/osm/pattani"
        / f"failure-{fail_link_number}"
    )
    assert not (destination / "extraction_manifest.json").exists()
    assert not list(destination.glob("*.tmp"))
