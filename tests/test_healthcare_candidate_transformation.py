from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import socket

import pytest

import src.transformation.healthcare_candidates as healthcare


@pytest.fixture(autouse=True)
def offline_only(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("external access is forbidden")
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, schema_drift: bool = False) -> Path:
    root = tmp_path / "raw"
    temporary = tmp_path / "source.csv"
    fields = list(healthcare.EXPECTED_FIELDS)
    if schema_drift:
        fields[-1] = "unexpected"
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index in range(healthcare.EXPECTED_SOURCE_RECORDS):
            row = {field: "safe" for field in fields}
            if not schema_drift:
                row[healthcare.ID_FIELD] = f"id-{index}"
                row[healthcare.ADDRESS_FIELD] = (
                    f"safe {healthcare.TARGET_TEXT}" if index < healthcare.EXPECTED_CANDIDATES else "safe"
                )
                row[healthcare.LATITUDE_FIELD] = "1.25"
                row[healthcare.LONGITUDE_FIELD] = "2.5"
            writer.writerow(row)
    raw = temporary.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(healthcare, "SOURCE_BYTES", len(raw))
    monkeypatch.setattr(healthcare, "SOURCE_SHA256", digest)
    artifact, metadata = healthcare.dga_healthcare_source(root)
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(raw)
    relative = artifact.relative_to(root).as_posix()
    metadata.write_text(json.dumps({
        "byte_count": len(raw), "sha256": digest,
        "resource_id": healthcare.SOURCE_RESOURCE_ID,
        "relative_artifact_path": relative,
    }), encoding="utf-8")
    return root


def test_deterministic_candidates_manifest_and_collision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _source(tmp_path, monkeypatch)
    first = healthcare.extract_pattani_healthcare_candidates(
        raw_root=raw, output_root=tmp_path / "one", minimum_free_bytes=1)
    second = healthcare.extract_pattani_healthcare_candidates(
        raw_root=raw, output_root=tmp_path / "two", minimum_free_bytes=1)
    assert first.data_path.read_bytes() == second.data_path.read_bytes()
    assert first.candidate_count == healthcare.EXPECTED_CANDIDATES
    assert first.source_record_count == healthcare.EXPECTED_SOURCE_RECORDS
    assert first.sha256 == hashlib.sha256(first.data_path.read_bytes()).hexdigest()
    records = [json.loads(line) for line in first.data_path.read_bytes().splitlines()]
    assert [record["candidate_sequence"] for record in records] == list(range(138))
    assert all(set(record) == {"candidate_sequence", "source_fields"} for record in records)
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["classification"] == "address_text_candidates"
    with pytest.raises(healthcare.HealthcareTransformationError) as raised:
        healthcare.extract_pattani_healthcare_candidates(
            raw_root=raw, output_root=tmp_path / "one", minimum_free_bytes=1)
    assert raised.value.category == "output_already_exists"


def test_schema_drift_publishes_no_data_or_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _source(tmp_path, monkeypatch, schema_drift=True)
    with pytest.raises(healthcare.HealthcareTransformationError) as raised:
        healthcare.extract_pattani_healthcare_candidates(
            raw_root=raw, output_root=tmp_path / "output", minimum_free_bytes=1)
    assert raised.value.category == "schema_drift"
    destination = tmp_path / "output/infrastructure/healthcare/dga/pattani-address-candidates-v1-20260925-01"
    assert not list(destination.glob("*.jsonl"))
    assert not (destination / "transformation_manifest.json").exists()


def test_failed_publication_is_safe_and_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _source(tmp_path, monkeypatch)
    monkeypatch.setattr(healthcare, "_link_no_replace", lambda *args: (_ for _ in ()).throw(
        healthcare.HealthcareTransformationError("publication_failed")))
    with pytest.raises(healthcare.HealthcareTransformationError) as raised:
        healthcare.extract_pattani_healthcare_candidates(
            raw_root=raw, output_root=tmp_path / "output", minimum_free_bytes=1)
    assert raised.value.category == "publication_failed"
    assert not raised.value.data_published
    assert "id-" not in repr(raised.value)
    destination = tmp_path / "output/infrastructure/healthcare/dga/pattani-address-candidates-v1-20260925-01"
    assert not list(destination.glob("*.tmp"))
    assert not (destination / "transformation_manifest.json").exists()
