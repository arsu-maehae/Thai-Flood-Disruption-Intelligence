from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.validation.exposure_outputs import verify_exposure_output, verify_temporal_exposure_output


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _output(root: Path) -> str:
    relative = "analysis/test"; directory = root / relative; directory.mkdir(parents=True)
    inputs = {}
    for name in ("flood", "roads", "healthcare"):
        path = root / f"inputs/{name}.json"; path.parent.mkdir(exist_ok=True); path.write_bytes(_json({"safe": name}))
        inputs[name] = {"manifest_relative_path": path.relative_to(root).as_posix(),
                        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "data_sha256": "0" * 64}
    outputs = {}
    for name in ("road_exposure.jsonl", "healthcare_exposure.jsonl"):
        content = _json({"analysis_sequence": 0, "source_sequence": 0,
                         "intersects_flood": True, "intersecting_polygon_count": 2})
        (directory / name).write_bytes(content)
        outputs[name] = {"relative_path": name, "byte_count": len(content),
                         "sha256": hashlib.sha256(content).hexdigest(), "record_count": 1}
    summary = _json({"policy_label": "exploratory_non_authoritative", "rejected_or_invalid_input_count": 0,
                     "roads": {"total": 1, "exposed": 1, "non_exposed": 0},
                     "healthcare": {"total": 1, "exposed": 1, "non_exposed": 0}})
    (directory / "exposure_summary.json").write_bytes(summary)
    outputs["exposure_summary.json"] = {"relative_path": "exposure_summary.json", "byte_count": len(summary),
        "sha256": hashlib.sha256(summary).hexdigest(), "record_count": 1}
    manifest = {"schema_version": "1.0", "policy_version": "exploratory_non_authoritative",
                "status": "complete", "inputs": inputs, "outputs": outputs}
    (directory / "analysis_manifest.json").write_bytes(_json(manifest))
    return relative


def test_read_only_verification_and_tamper_detection(tmp_path: Path) -> None:
    relative = _output(tmp_path); before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = verify_exposure_output(tmp_path, relative)
    assert result.complete and result.road_count == result.healthcare_count == 1
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    road = tmp_path / relative / "road_exposure.jsonl"; road.write_bytes(road.read_bytes() + b"{}\n")
    assert "integrity_failed" in verify_exposure_output(tmp_path, relative).issue_categories


def test_incomplete_unexpected_and_temporary_outputs(tmp_path: Path) -> None:
    relative = _output(tmp_path); directory = tmp_path / relative
    (directory / "analysis_manifest.json").unlink()
    assert "manifest_invalid" in verify_exposure_output(tmp_path, relative).issue_categories
    relative = _output(tmp_path / "second"); directory = tmp_path / "second" / relative
    (directory / ".left.tmp").write_bytes(b""); (directory / "extra").write_bytes(b"")
    issues = verify_exposure_output(tmp_path / "second", relative).issue_categories
    assert "temporary_remnant" in issues and "unexpected_entry" in issues


def _temporal(root: Path) -> str:
    relative = "analysis/temporal"; directory = root / relative; directory.mkdir(parents=True)
    years = [f"y_{year}" for year in range(2011, 2025)]
    inputs = {}
    for name in ("phase4a", "flood", "roads", "healthcare"):
        path = root / f"inputs/{name}-temporal.json"; path.parent.mkdir(exist_ok=True); path.write_bytes(_json({"safe": name}))
        inputs[name] = {"manifest_relative_path": path.relative_to(root).as_posix(),
                        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    summary = {"policy_label": "exploratory_non_authoritative", "years": years,
               "phase4a_reconciled": True, "road_total": 2, "healthcare_total": 1,
               "road_ever_exposed": 1, "healthcare_ever_exposed": 1,
               "annual_road_exposed": {year: (1 if index == 0 else 0) for index, year in enumerate(years)},
               "annual_healthcare_exposed": {year: (1 if index == 0 else 0) for index, year in enumerate(years)}}
    files = {"annual_exposure_summary.json": _json(summary),
             "frequency_consistency.json": _json({"feature_count": 2, "match_count": 1, "mismatch_count": 1,
                 "missing_or_invalid_count": 0, "per_year_active_feature_counts": {year: 0 for year in years}}),
             "road_category_annual_exposure.csv": (",".join(["highway_category", *years, "ever_exposed", "total_records"]) + "\nprimary,1," + ",".join(["0"] * 13) + ",1,2\n").encode(),
             "healthcare_annual_exposure.csv": (",".join(["scope", *years, "ever_exposed", "total_records"]) + "\naddress_text_candidates,1," + ",".join(["0"] * 13) + ",1,1\n").encode()}
    outputs = {}
    for name, content in files.items():
        (directory / name).write_bytes(content); outputs[name] = {"relative_path": name, "byte_count": len(content),
            "sha256": hashlib.sha256(content).hexdigest(), "record_count": 1}
    manifest = {"schema_version": "1.0", "policy_version": "exploratory_non_authoritative",
                "status": "complete", "inputs": inputs, "outputs": outputs}
    (directory / "analysis_manifest.json").write_bytes(_json(manifest)); return relative


def test_temporal_verifier_is_read_only_and_rejects_tampering(tmp_path: Path) -> None:
    relative = _temporal(tmp_path); before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = verify_temporal_exposure_output(tmp_path, relative)
    assert result.complete and result.road_count == 2 and result.healthcare_count == 1
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    path = tmp_path / relative / "healthcare_annual_exposure.csv"; path.write_bytes(path.read_bytes() + b"bad")
    assert "integrity_failed" in verify_temporal_exposure_output(tmp_path, relative).issue_categories
