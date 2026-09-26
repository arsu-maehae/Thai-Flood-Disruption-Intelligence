from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.validation.exposure_outputs import verify_exposure_output


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
