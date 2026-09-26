from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket

import pytest

import src.reporting.exposure_report as report


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network")))
    monkeypatch.setattr("dotenv.main.dotenv_values", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("configuration")))


def _data(category: str = "primary") -> dict[str, object]:
    return {"report_id": report.REPORT_ID, "policy_label": "exploratory_non_authoritative",
        "crs_status": "provider_unverified_exploratory_interpretation",
        "analysis_ids": {"phase4a": "exploratory-exposure-v1-20260925-01", "phase4b": "temporal-exposure-v1-20260926-01"},
        "headline": {"roads": {"total": 32358, "exposed": 4919, "non_exposed": 27439},
                     "healthcare": {"total": 138, "exposed": 18, "non_exposed": 120}},
        "years": list(range(2011, 2025)),
        "annual_road_exposed": [74, 15, 1009, 232, 15, 42, 4042, 0, 121, 548, 698, 648, 629, 840],
        "annual_healthcare_exposed": [0, 0, 2, 1, 0, 0, 15, 0, 0, 3, 3, 4, 3, 6],
        "road_categories": [{"category": category, "ever_exposed": 4919, "total_records": 32358}],
        "frequency_consistency": {"feature_count": 112073, "match_count": 112073,
            "mismatch_count": 0, "missing_or_invalid_count": 0,
            "relationship_status": "observed_structural_relationship_only"},
        "caveats": ["safe"]}


def _inputs(root: Path) -> dict[str, object]:
    result = {}
    for key, analysis_id in (("phase4a", "exploratory-exposure-v1-20260925-01"),
                             ("phase4b", "temporal-exposure-v1-20260926-01")):
        path = root / f"inputs/{key}/analysis_manifest.json"; path.parent.mkdir(parents=True)
        raw = json.dumps({"analysis_id": analysis_id, "status": "complete"}, sort_keys=True).encode()
        path.write_bytes(raw); result[key] = {"analysis_id": analysis_id,
            "manifest_relative_path": path.relative_to(root).as_posix(),
            "manifest_sha256": hashlib.sha256(raw).hexdigest(), "verified": True}
    return result


def _publish(root: Path, monkeypatch: pytest.MonkeyPatch, *, category: str = "primary") -> report.ExposureReportResult:
    data = _data(category); inputs = _inputs(root)
    monkeypatch.setattr(report, "_load_inputs", lambda candidate: (data, inputs))
    monkeypatch.setattr(report, "verify_exposure_output", lambda *args, **kwargs: type("V", (), {"complete": True})())
    monkeypatch.setattr(report, "verify_temporal_exposure_output", lambda *args, **kwargs: type("V", (), {"complete": True})())
    return report.publish_exposure_report(processed_root=root,
        generated_at=datetime(2026, 9, 27, tzinfo=timezone.utc))


def test_deterministic_valid_report_and_read_only_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = _publish(tmp_path / "one", monkeypatch)
    second = _publish(tmp_path / "two", monkeypatch)
    assert first.html_path.read_bytes() == second.html_path.read_bytes()
    assert first.summary_path.read_bytes() == second.summary_path.read_bytes()
    verified = report.verify_exposure_report(tmp_path / "two")
    assert verified.complete and verified.road_total == 32358 and verified.healthcare_total == 138
    before = {p: p.read_bytes() for p in (tmp_path / "two").rglob("*") if p.is_file()}
    report.verify_exposure_report(tmp_path / "two")
    assert before == {p: p.read_bytes() for p in (tmp_path / "two").rglob("*") if p.is_file()}


def test_html_escaping_and_no_external_or_sensitive_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _publish(tmp_path, monkeypatch, category="<private&category>")
    html = result.html_path.read_text(encoding="utf-8")
    assert "&lt;private&amp;category&gt;" in html and "<private&category>" not in html
    lowered = html.casefold()
    assert all(token not in lowered for token in ("http://", "https://", "<script src", "<iframe", "api-key", "coordinates"))


@pytest.mark.parametrize("mutation", ["road_total", "year", "frequency", "category"])
def test_invalid_aggregate_contract_is_rejected(mutation: str) -> None:
    data = _data()
    if mutation == "road_total": data["headline"]["roads"]["total"] = 32357
    elif mutation == "year": data["years"][0] = 2010
    elif mutation == "frequency": data["frequency_consistency"]["mismatch_count"] = 1
    else: data["road_categories"][0]["ever_exposed"] = 4918
    with pytest.raises(report.ExposureReportError) as raised:
        report._validate_summary(data)
    assert raised.value.category == "input_invalid"


def test_collision_and_cleanup_publication_states(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "index.html"; path.write_bytes(b"old")
    with pytest.raises(report.ExposureReportError) as collision:
        report._publish(path, b"new")
    assert collision.value.category == "output_exists" and path.read_bytes() == b"old"
    original = Path.unlink; calls = 0
    def fail_once(candidate: Path, *args: object, **kwargs: object) -> None:
        nonlocal calls
        if candidate.name.endswith(".tmp") and calls == 0:
            calls += 1; raise OSError("secret")
        original(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_once)
    manifest = tmp_path / "report_manifest.json"
    with pytest.raises(report.ExposureReportError) as cleanup:
        report._publish(manifest, b"{}", completion=True)
    assert cleanup.value.category == "cleanup_failed"
    assert cleanup.value.output_published and cleanup.value.manifest_published
    assert not cleanup.value.cleanup_failed and manifest.read_bytes() == b"{}"
    assert "secret" not in repr(cleanup.value)


def test_prepublication_failure_cleans_temporary_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report.os, "link", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("private")))
    path = tmp_path / "index.html"
    with pytest.raises(report.ExposureReportError) as raised:
        report._publish(path, b"content")
    assert raised.value.category == "publication_failed"
    assert not raised.value.output_published and not raised.value.manifest_published
    assert not raised.value.cleanup_failed and not path.exists()
    assert not list(tmp_path.glob("*.tmp")) and "private" not in repr(raised.value)


def test_manifest_is_last_and_absent_after_summary_publication_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _data(); inputs = _inputs(tmp_path)
    monkeypatch.setattr(report, "_load_inputs", lambda candidate: (data, inputs))
    original = report._publish
    def fail_summary(path: Path, content: bytes, *, completion: bool = False) -> None:
        if path.name == "report_summary.json":
            raise report.ExposureReportError("publication_failed")
        original(path, content, completion=completion)
    monkeypatch.setattr(report, "_publish", fail_summary)
    with pytest.raises(report.ExposureReportError):
        report.publish_exposure_report(processed_root=tmp_path,
            generated_at=datetime(2026, 9, 27, tzinfo=timezone.utc))
    directory = tmp_path / report.OUTPUT_RELATIVE
    assert (directory / "index.html").is_file()
    assert not (directory / "report_manifest.json").exists()


def test_incomplete_corrupt_and_unexpected_verification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _publish(tmp_path, monkeypatch)
    result.manifest_path.unlink()
    assert "manifest_invalid" in report.verify_exposure_report(tmp_path).issue_categories
    result.manifest_path.write_bytes(b"{}")
    assert "manifest_invalid" in report.verify_exposure_report(tmp_path).issue_categories
    result.manifest_path.unlink(); _publish(tmp_path / "other", monkeypatch)
    directory = tmp_path / "other" / report.OUTPUT_RELATIVE
    (directory / "unexpected").write_bytes(b"")
    assert "unexpected_entry" in report.verify_exposure_report(tmp_path / "other").issue_categories
