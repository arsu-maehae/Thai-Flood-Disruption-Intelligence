from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.validation.infrastructure_outputs import verify_infrastructure_output


def _publish(root: Path, kind: str) -> str:
    raw = root / "raw"; raw.mkdir(exist_ok=True)
    source = raw / "source.bin"; source.write_bytes(b"source")
    source_hash = hashlib.sha256(b"source").hexdigest()
    directory = root / kind
    directory.mkdir(parents=True)
    if kind == "roads":
        record = {"feature_sequence": 0, "way_sequence": 0, "segment_sequence": 0,
                  "osm_way_id": 1, "highway": "safe", "geometry": {"type": "LineString", "coordinates": []}}
        policy = "pattani-osm-highways-observed-v1"; manifest_name = "extraction_manifest.json"
        count_key = "segment_count"
    else:
        record = {"candidate_sequence": 0, "source_fields": {"safe": "value"}}
        policy = "pattani-address-text-candidates-v1"; manifest_name = "transformation_manifest.json"
        count_key = "record_count"
    content = json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    digest = hashlib.sha256(content).hexdigest(); data = directory / f"data-{digest}.jsonl"; data.write_bytes(content)
    manifest = {"schema_version": "1.0", "policy_version": policy, "status": "complete",
                "source": {"relative_artifact_path": "source.bin", "sha256": source_hash,
                           "byte_count": len(b"source")},
                "output": {"relative_path": data.relative_to(root).as_posix(), "byte_count": len(content),
                           "sha256": digest, count_key: 1}}
    (directory / manifest_name).write_text(json.dumps(manifest), encoding="utf-8")
    return kind


def test_verifies_both_output_contracts(tmp_path: Path) -> None:
    for kind in ("roads", "healthcare"):
        relative = _publish(tmp_path, kind)
        result = verify_infrastructure_output(tmp_path, relative, raw_root=tmp_path / "raw")
        assert result.complete
        assert result.record_count == 1
        assert result.issue_categories == ()


def test_detects_hash_count_unexpected_and_temporary_state(tmp_path: Path) -> None:
    relative = _publish(tmp_path, "roads")
    directory = tmp_path / relative
    data = next(directory.glob("*.jsonl")); data.write_bytes(data.read_bytes() + b"{}\n")
    (directory / ".left.tmp").write_bytes(b"")
    (directory / "unexpected.txt").write_bytes(b"")
    result = verify_infrastructure_output(tmp_path, relative, raw_root=tmp_path / "raw")
    assert set(result.issue_categories) >= {"temporary_remnant", "unexpected_entry"}
    (directory / ".left.tmp").unlink(); (directory / "unexpected.txt").unlink()
    result = verify_infrastructure_output(tmp_path, relative, raw_root=tmp_path / "raw")
    assert set(result.issue_categories) >= {"hash_mismatch", "count_mismatch"}


def test_rejects_unsafe_or_incomplete_input(tmp_path: Path) -> None:
    assert verify_infrastructure_output(tmp_path, "../outside", raw_root=tmp_path / "raw").issue_categories == ("invalid_input",)
    (tmp_path / "empty").mkdir()
    assert verify_infrastructure_output(tmp_path, "empty", raw_root=tmp_path / "raw").issue_categories == ("output_incomplete",)
