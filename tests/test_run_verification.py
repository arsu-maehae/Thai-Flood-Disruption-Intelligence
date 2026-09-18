"""Synthetic inactive runs only; never inspect repository data or real secrets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest
import requests

from src import configuration
from src.configuration import GistdaConfig, OFFICIAL_GISTDA_API_BASE_URL
from src.ingestion.flood_frequency import ingest_pattani_page
from src.ingestion.gistda_client import GistdaResponse
from src.ingestion.run_manifest import (
    PageJournalRecord, RunCounts, RunJournal, UnjournaledPageReference,
)
from src.ingestion.run_verification import verify_run

KEY = "dummy-verifier-secret+with space"
TIME = datetime(2026, 9, 18, 2, 3, 4, 567890, tzinfo=timezone.utc)
RUN_ID = "synthetic-run"
RUNS = Path("gistda/flood_freq/pattani/runs")


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("network/configuration access prohibited")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(requests, "request", forbidden)
    monkeypatch.setattr(configuration, "load_config", forbidden)
    monkeypatch.setattr(configuration, "dotenv_values", forbidden)
    original_read = Path.read_bytes
    original_open = Path.open

    def allowed(path):
        assert path.name != ".env"
        assert path.absolute().is_relative_to(tmp_path.absolute())

    def read(path, *args, **kwargs):
        allowed(path)
        return original_read(path, *args, **kwargs)

    def open_file(path, *args, **kwargs):
        allowed(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(Path, "open", open_file)


class FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def get_flood_frequency(self, **kwargs):
        return GistdaResponse(json.dumps(self.payload).encode(), 200, "application/geo+json")


def source(root, *, index=0, ids=None, limit=2, matched=3, absent=False):
    ids = [f"synthetic-{index}"] if ids is None else ids
    payload = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "id": value, "properties": {}} for value in ids],
        "numberReturned": len(ids),
        "links": [{"rel": "self", "href": "https://example.test/page?api_key=" +
                   quote_plus(KEY) + "&offset=" + str(index * limit)}],
    }
    if not absent:
        payload["numberMatched"] = matched
    result = ingest_pattani_page(
        config=GistdaConfig(OFFICIAL_GISTDA_API_BASE_URL, KEY, "94"),
        client=FakeClient(payload), output_root=root, limit=limit, offset=index * limit,
        retrieved_at=TIME + timedelta(seconds=index),
    )
    return PageJournalRecord(
        index, index * limit, limit, result.artifact_path, result.metadata_path,
        result.stored_artifact_sha256, result.number_returned,
        result.number_matched_present, result.number_matched,
        result.missing_feature_id_count == 0,
    )


def build(root, *, coordinated=True, terminal="complete", pages=None,
          reference_stage=None, absent=False):
    journal = RunJournal.start(
        output_root=root, run_id=RUN_ID, expected_offsets=(0, 2, 4, 6),
        started_at=TIME, api_key=KEY,
        counts=RunCounts(0, 0, 0, 0) if coordinated else None,
    )
    pages = [["first", "second"], ["third"]] if pages is None else pages
    for index, ids in enumerate(pages):
        record = source(root, index=index, ids=ids, absent=absent)
        n = index + 1
        journal.append_page(record, counts=RunCounts(n, n, n, n) if coordinated else None)
    n = len(pages)
    if terminal == "complete":
        journal.complete(completed_at=TIME + timedelta(minutes=1),
                         stop_reason="empty_page" if not pages[-1] else "partial_page",
                         observed_number_matched=None if absent else 3,
                         duplicate_id_check_complete=all(all(isinstance(i, str) and i for i in ids) for ids in pages),
                         counts=RunCounts(n, n, n, n) if coordinated else None)
    elif terminal == "failed":
        reference = None
        counts = RunCounts(n + 1, n, n, n) if coordinated else None
        if reference_stage:
            record = source(root, index=n, ids=["failed-page"], absent=absent)
            reference = UnjournaledPageReference.from_page(
                record=record, outcome_stage=reference_stage, output_root=root, api_key=KEY,
            )
            counts = RunCounts(n + 1, n + 1, n + (reference_stage == "validated_unjournaled"), n)
        journal.fail(failed_at=TIME + timedelta(minutes=1), failure_category="synthetic_failure",
                     counts=counts, unjournaled_page=reference)
    return root / RUNS / RUN_ID


def load(path):
    return json.loads(path.read_bytes())


def mutate(path, **changes):
    value = load(path)
    value.update(changes)
    path.write_text(json.dumps(value), encoding="utf-8")


def verify(root, **kwargs):
    return verify_run(root, RUN_ID, api_key=kwargs.pop("api_key", KEY), **kwargs)


def invalid(root, category=None):
    report = verify(root)
    assert report.status == "invalid"
    if category:
        assert category in report.issue_categories
    assert_safe(report)
    return report


def assert_safe(report, *offending):
    text = repr(report) + str(report) + json.dumps(report.to_dict())
    for value in (KEY, quote(KEY, safe=""), quote(quote(KEY, safe=""), safe=""), *offending):
        assert value not in text


def snapshot(root):
    return tuple(sorted((path.relative_to(root).as_posix(), path.is_dir(),
                         path.stat().st_mtime_ns, None if path.is_dir() else path.read_bytes())
                        for path in [root, *root.rglob("*")]))


def test_complete_coordinated_multi_page_read_only(tmp_path):
    build(tmp_path)
    before = snapshot(tmp_path)
    report = verify(tmp_path)
    assert report.status == report.terminal_kind == "complete"
    assert report.journal_page_count == 2 and report.observed_total_features == 3
    assert report.observed_number_matched_present is True and report.observed_number_matched == 3
    assert report.last_published_counts.to_dict() == dict(zip(
        ("attempted_request_count", "persisted_page_count", "validated_page_count", "journaled_page_count"), (2, 2, 2, 2)))
    assert report.counts_final and report.configured_key_check_complete
    assert not report.original_response_independently_verified
    assert report.issue_categories == ()
    assert report == verify(tmp_path)
    assert snapshot(tmp_path) == before
    assert_safe(report, "first", "second", "third", "https://example.test")
    with pytest.raises(FrozenInstanceError):
        report.status = "failed"
    with pytest.raises(FrozenInstanceError):
        report.last_published_counts.journaled_page_count = 99


@pytest.mark.parametrize("stage", [None, "persisted_unvalidated", "validated_unjournaled"])
def test_failed_coordinated_lineage(tmp_path, stage):
    build(tmp_path, terminal="failed", pages=[["first", "second"]], reference_stage=stage)
    before = snapshot(tmp_path)
    report = verify(tmp_path)
    assert report.status == report.terminal_kind == "failed"
    assert report.last_published_counts.attempted_request_count == 2
    assert report.last_published_counts.persisted_page_count == (1 if stage is None else 2)
    assert report.journal_page_count == 1
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("terminal", ["complete", "failed", None])
def test_standalone(tmp_path, terminal):
    build(tmp_path, coordinated=False, terminal=terminal,
          pages=[["first"]] if terminal == "complete" else [["first", "second"]])
    report = verify(tmp_path)
    assert report.status == (terminal or "interrupted")
    assert report.last_published_counts.attempted_request_count is None
    assert report.last_published_counts.persisted_page_count is None
    assert report.last_published_counts.validated_page_count is None
    assert report.last_published_counts.journaled_page_count == 1


@pytest.mark.parametrize("pages", [[], [["first", "second"]]])
def test_interrupted_only_published_snapshot(tmp_path, pages):
    build(tmp_path, terminal=None, pages=pages)
    report = verify(tmp_path)
    assert report.status == "interrupted" and report.terminal_kind is None
    assert report.journal_page_count == len(pages)
    assert report.last_published_counts.attempted_request_count == len(pages)
    assert not report.counts_final


def test_absent_and_missing_start(tmp_path):
    assert verify(tmp_path).status == "not_found"
    directory = tmp_path / RUNS / RUN_ID
    directory.mkdir(parents=True)
    invalid(tmp_path, "missing_start_record")


def test_both_terminals(tmp_path):
    directory = build(tmp_path)
    (directory / "run_failed.json").write_bytes((directory / "run_complete.json").read_bytes())
    invalid(tmp_path, "multiple_terminal_records")


@pytest.mark.parametrize("family", ["start", "page", "terminal", "artifact", "metadata"])
def test_duplicate_keys_all_families(tmp_path, family):
    directory = build(tmp_path)
    page = load(directory / "page_000000.json")
    path = {
        "start": directory / "run_started.json", "page": directory / "page_000000.json",
        "terminal": directory / "run_complete.json",
        "artifact": tmp_path / page["relative_artifact_path"],
        "metadata": tmp_path / page["relative_metadata_path"],
    }[family]
    content = path.read_bytes()
    path.write_bytes(b'{"duplicate":1,"duplicate":2,' + content.lstrip()[1:])
    invalid(tmp_path, "duplicate_json_keys")


@pytest.mark.parametrize("filename", ["run_started.json", "page_000000.json", "run_complete.json"])
@pytest.mark.parametrize("field,value,issue", [
    ("journal_schema_version", "1.1", "unsupported_schema"),
    ("pipeline_policy_version", "99.0", "unsupported_policy_version"),
    ("run_id", "different", "run_id_mismatch"),
])
def test_versions_and_record_run_ids(tmp_path, filename, field, value, issue):
    directory = build(tmp_path)
    mutate(directory / filename, **{field: value})
    invalid(tmp_path, issue)


@pytest.mark.parametrize("changes,issue", [
    ({"status": "complete"}, "invalid_start_record"),
    ({"writer_model": "multiple"}, "invalid_start_record"),
    ({"expected_offsets": [0, 0]}, "invalid_expected_offsets"),
    ({"expected_offsets": [True, 2]}, "invalid_expected_offsets"),
    ({"expected_offsets": []}, "invalid_expected_offsets"),
    ({"expected_offsets": [2, 4]}, "invalid_expected_offsets"),
    ({"pagination_policy": "unknown"}, "unsupported_pagination_policy"),
    ({"started_at_utc": "2026-09-18T02:03:04"}, "invalid_timestamp"),
])
def test_start_validation(tmp_path, changes, issue):
    directory = build(tmp_path)
    mutate(directory / "run_started.json", **changes)
    invalid(tmp_path, issue)


@pytest.mark.parametrize("changes,issue", [
    ({"page_index": 1}, "invalid_page_index"),
    ({"page_index": False}, "invalid_page_index"),
    ({"requested_offset": 1}, "offset_sequence_mismatch"),
    ({"requested_limit": 0}, "invalid_page_limit"),
    ({"requested_limit": 10001}, "invalid_page_limit"),
    ({"requested_limit": True}, "invalid_page_limit"),
    ({"number_returned": 3}, "invalid_number_returned"),
    ({"number_returned": True}, "invalid_number_returned"),
    ({"number_matched_present": "true"}, "invalid_number_matched"),
    ({"number_matched": True}, "invalid_number_matched"),
    ({"number_matched_present": False}, "invalid_number_matched"),
    ({"feature_id_check_complete": 1}, "invalid_id_completeness"),
    ({"stored_artifact_sha256": "A" * 64}, "invalid_digest"),
])
def test_page_validation(tmp_path, changes, issue):
    directory = build(tmp_path)
    mutate(directory / "page_000000.json", **changes)
    invalid(tmp_path, issue)


@pytest.mark.parametrize("name", ["page_0.json", "page_000002.json"])
def test_page_filename_and_gap(tmp_path, name):
    directory = build(tmp_path)
    (directory / "page_000000.json").rename(directory / name)
    invalid(tmp_path, "invalid_page_filename")


def test_offset_progression(tmp_path):
    directory = build(tmp_path)
    mutate(directory / "run_started.json", expected_offsets=[0, 3, 6])
    mutate(directory / "page_000001.json", requested_offset=3)
    invalid(tmp_path, "offset_progression_mismatch")


def test_page_after_partial(tmp_path):
    directory = build(tmp_path, terminal=None, pages=[["first"]])
    content = load(directory / "page_000000.json")
    content.update(page_index=1, requested_offset=2)
    (directory / "page_000001.json").write_text(json.dumps(content), encoding="utf-8")
    invalid(tmp_path, "page_after_stop_page")


@pytest.mark.parametrize("changes", [{"number_matched": 4}, {"number_matched_present": False, "number_matched": None}])
def test_changing_matched(tmp_path, changes):
    directory = build(tmp_path)
    mutate(directory / "page_000001.json", **changes)
    invalid(tmp_path, "number_matched_changed")


def test_absent_matched_and_empty_completion(tmp_path):
    build(tmp_path, pages=[[]], absent=True)
    report = verify(tmp_path)
    assert report.status == "complete"
    assert report.observed_number_matched_present is False and report.observed_number_matched is None
    assert report.observed_total_features == 0


@pytest.mark.parametrize("changes,issue", [
    ({"attempted_request_count": True}, "invalid_counts"),
    ({"attempted_request_count": -1}, "invalid_counts"),
    ({"persisted_page_count": None}, "invalid_counts"),
    ({"validated_page_count": 0}, "invalid_counts"),
    ({"journaled_page_count": 0}, "invalid_counts"),
    ({"attempted_request_count": 2}, "invalid_counts"),
    ({"request_count": 99}, "request_count_mismatch"),
    ({"request_count": True}, "request_count_mismatch"),
    ({"request_count_semantics": "unknown"}, "count_semantics_mismatch"),
])
def test_counts(tmp_path, changes, issue):
    directory = build(tmp_path)
    mutate(directory / "page_000000.json", **changes)
    invalid(tmp_path, issue)


def test_accounting_mode_change(tmp_path):
    directory = build(tmp_path)
    mutate(directory / "page_000000.json", attempted_request_count=None, persisted_page_count=None,
           validated_page_count=None, request_count=None,
           request_count_semantics="Attempted application-level requests are unknown")
    invalid(tmp_path, "accounting_mode_changed")


@pytest.mark.parametrize("changes,issue", [
    ({"page_count": 99}, "completion_summary_mismatch"),
    ({"total_features": 99}, "completion_summary_mismatch"),
    ({"stop_reason": "empty_page"}, "stop_reason_mismatch"),
    ({"observed_number_matched": 99}, "number_matched_summary_mismatch"),
    ({"observed_number_matched_present": False}, "invalid_number_matched"),
    ({"duplicate_id_check_complete": 1}, "id_completeness_summary_mismatch"),
    ({"completed_at_utc": "2020-01-01T00:00:00Z"}, "terminal_timestamp_mismatch"),
    ({"started_at_utc": "2020-01-01T00:00:00Z"}, "terminal_timestamp_mismatch"),
])
def test_completion_summaries(tmp_path, changes, issue):
    directory = build(tmp_path)
    mutate(directory / "run_complete.json", **changes)
    invalid(tmp_path, issue)


@pytest.mark.parametrize("changes,issue", [
    ({"successful_page_count": 99}, "failure_summary_mismatch"),
    ({"last_successful_offset": 99}, "failure_summary_mismatch"),
    ({"failure_category": "bad category"}, "invalid_failure_category"),
    ({"attempted_request_count": 3, "request_count": 3}, "invalid_failure_counts"),
    ({"unjournaled_page": {}}, "invalid_unjournaled_lineage"),
    ({"failed_at_utc": "2020-01-01T00:00:00Z"}, "terminal_timestamp_mismatch"),
])
def test_failure_summaries(tmp_path, changes, issue):
    directory = build(tmp_path, terminal="failed", pages=[["first", "second"]])
    mutate(directory / "run_failed.json", **changes)
    invalid(tmp_path, issue)


@pytest.mark.parametrize("change", ["missing", "stage", "offset", "path", "hash", "observation"])
def test_invalid_lineage(tmp_path, change):
    directory = build(tmp_path, terminal="failed", pages=[["first", "second"]],
                      reference_stage="validated_unjournaled")
    terminal_path = directory / "run_failed.json"
    terminal = load(terminal_path)
    if change == "missing":
        del terminal["unjournaled_page"]
    else:
        reference = terminal["unjournaled_page"]
        reference.update({
            "stage": {"outcome_stage": "persisted_unvalidated"},
            "offset": {"requested_offset": 99},
            "path": {"relative_artifact_path": "../outside.json"},
            "hash": {"stored_artifact_sha256": "b" * 64},
            "observation": {"number_returned": 0},
        }[change])
    terminal_path.write_text(json.dumps(terminal), encoding="utf-8")
    invalid(tmp_path)


@pytest.mark.parametrize("family", ["artifact", "metadata"])
@pytest.mark.parametrize("damage", ["missing", "malformed", "array", "nonfinite", "overflow"])
def test_source_document_damage(tmp_path, family, damage):
    directory = build(tmp_path)
    page = load(directory / "page_000000.json")
    path = tmp_path / page[f"relative_{family}_path"]
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes({"malformed": b'{', "array": b'[]', "nonfinite": b'{"x":NaN}',
                          "overflow": b'{"x":1e999}'}[damage])
    invalid(tmp_path, {"missing": "filesystem_error", "malformed": "invalid_json",
                       "array": "nonobject_document", "nonfinite": "nonfinite_json_number",
                       "overflow": "nonfinite_json_number"}[damage])


@pytest.mark.parametrize("changes,issue", [
    ({"stored_artifact_sha256": "b" * 64}, "stored_hash_mismatch"),
    ({"stored_artifact_byte_count": 0}, "stored_byte_count_mismatch"),
    ({"stored_artifact_byte_count": True}, "stored_byte_count_mismatch"),
    ({"relative_stored_artifact_path": "other.json"}, "artifact_path_mismatch"),
    ({"original_response_sha256": "invalid"}, "invalid_provenance"),
    ({"original_response_byte_count": True}, "invalid_provenance"),
    ({"sanitization_applied": False}, "invalid_provenance"),
    ({"original_response_persisted": True}, "invalid_provenance"),
    ({"provider": "unknown"}, "invalid_provenance"),
    ({"dataset": "unknown"}, "invalid_provenance"),
    ({"endpoint_path": "/other"}, "invalid_provenance"),
    ({"removed_credential_count": 0}, "invalid_provenance"),
    ({"removed_credential_field_names": ["unknown"]}, "invalid_provenance"),
    ({"removed_credential_query_parameter_names": ["API-Key"]}, "invalid_provenance"),
    ({"request_parameters": {"pv_idn": "95", "limit": 2, "offset": 0}}, "request_parameters_mismatch"),
    ({"observed_number_returned": 99}, "source_observations_mismatch"),
    ({"top_level_feature_id_check_complete": False}, "source_observations_mismatch"),
])
def test_metadata_integrity_provenance(tmp_path, changes, issue):
    directory = build(tmp_path)
    page = load(directory / "page_000000.json")
    mutate(tmp_path / page["relative_metadata_path"], **changes)
    invalid(tmp_path, issue)


def test_filename_hash_prefix(tmp_path):
    directory = build(tmp_path)
    page_path = directory / "page_000000.json"
    page = load(page_path)
    old = tmp_path / page["relative_artifact_path"]
    new = old.with_name(old.name.replace(page["stored_artifact_sha256"][:12], "000000000000"))
    old.rename(new)
    relative = new.relative_to(tmp_path).as_posix()
    mutate(page_path, relative_artifact_path=relative)
    mutate(tmp_path / page["relative_metadata_path"], relative_stored_artifact_path=relative)
    invalid(tmp_path, "artifact_filename_mismatch")


@pytest.mark.parametrize("relative", ["../outside.json", "C:\\outside.json", "/outside.json"])
def test_external_paths_not_followed(tmp_path, relative):
    directory = build(tmp_path)
    mutate(directory / "page_000000.json", relative_artifact_path=relative)
    invalid(tmp_path, "external_path")


@pytest.mark.parametrize("target_kind", ["journal", "artifact", "metadata", "directory"])
def test_symlinks(tmp_path, target_kind):
    directory = build(tmp_path)
    page = load(directory / "page_000000.json")
    path = {"journal": directory / "page_000000.json", "artifact": tmp_path / page["relative_artifact_path"],
            "metadata": tmp_path / page["relative_metadata_path"], "directory": directory}[target_kind]
    moved = path.with_name(path.name + ".moved")
    path.rename(moved)
    try:
        path.symlink_to(moved, target_is_directory=target_kind == "directory")
    except (OSError, NotImplementedError):
        pytest.skip("platform cannot create symlinks")
    if target_kind != "directory":
        # Remove the extra run-directory name without deleting the test target.
        if moved.parent == directory:
            relocated = tmp_path / "moved-target.json"
            path.unlink()
            moved.rename(relocated)
            path.symlink_to(relocated)
    invalid(tmp_path, "symlink_rejected")


@pytest.mark.parametrize("terminal", ["complete", "failed"])
def test_temporary_remnants_preserve_terminal(tmp_path, terminal):
    directory = build(tmp_path, terminal=terminal,
                      pages=[["first"]] if terminal == "complete" else [["first", "second"]])
    (directory / f".run_{terminal}.json.dummy.tmp").write_bytes(b"unpublished data")
    before = snapshot(tmp_path)
    report = verify(tmp_path)
    assert report.status == terminal and report.terminal_kind == terminal
    assert report.temporary_remnant_count == 1 and "temporary_remnants" in report.issue_categories
    assert snapshot(tmp_path) == before


def test_unexpected_name_secret_never_reported(tmp_path):
    directory = build(tmp_path)
    # Use a filesystem-safe dummy credential distinct from the standard dummy key.
    secret = "dummy-unexpected-name-secret"
    (directory / secret).write_bytes(b"not a journal record")
    report = verify_run(tmp_path, RUN_ID, api_key=secret)
    assert report.status == "invalid" and report.unexpected_entry_count == 0
    assert "credential_rejected" in report.issue_categories
    assert_safe(report, secret)


@pytest.mark.parametrize("terminal", ["complete", "failed"])
@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_credential_temporary_filename_rejected_read_only(tmp_path, monkeypatch, terminal, depth):
    directory = build(tmp_path, terminal=terminal,
                      pages=[["first"]] if terminal == "complete" else [["first", "second"]])
    spelling = KEY
    for _ in range(depth):
        spelling = quote(spelling, safe="")
    filename = f".run_{terminal}.json.{spelling}.tmp"
    temporary = directory / filename
    temporary.write_bytes(b"synthetic temporary contents must not be inspected")
    before = snapshot(tmp_path)
    original_read = Path.read_bytes
    original_open = Path.open

    def read(path, *args, **kwargs):
        assert path != temporary, "temporary contents accessed"
        return original_read(path, *args, **kwargs)

    def open_file(path, *args, **kwargs):
        assert path != temporary, "temporary contents accessed"
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as checks:
        checks.setattr(Path, "read_bytes", read)
        checks.setattr(Path, "open", open_file)
        report = invalid(tmp_path, "credential_rejected")
    assert not report.configured_key_check_complete
    assert report.temporary_remnant_count == 0  # Rejection precedes classification.
    assert_safe(report, filename, spelling)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("terminal", ["complete", "failed"])
def test_temporary_filename_without_supplied_key(tmp_path, monkeypatch, terminal):
    directory = build(tmp_path, terminal=terminal,
                      pages=[["first"]] if terminal == "complete" else [["first", "second"]])
    temporary = directory / f".run_{terminal}.json.{quote(KEY, safe='')}.tmp"
    temporary.write_bytes(b"synthetic uninspected temporary contents")
    before = snapshot(tmp_path)
    original_read = Path.read_bytes

    def read(path, *args, **kwargs):
        assert path != temporary, "temporary contents accessed"
        return original_read(path, *args, **kwargs)

    with monkeypatch.context() as checks:
        checks.setattr(Path, "read_bytes", read)
        report = verify(tmp_path, api_key=None)
    assert report.status == report.terminal_kind == terminal
    assert report.temporary_remnant_count == 1
    assert not report.configured_key_check_complete
    assert "configured_key_unverified" in report.issue_categories
    assert_safe(report, temporary.name)
    assert snapshot(tmp_path) == before


def encoded(value, depth):
    for _ in range(depth):
        value = "".join(f"%{byte:02X}" for byte in value.encode())
    return value


@pytest.mark.parametrize("name", ["api_key", "API-Key", "AUTHORIZATION"])
@pytest.mark.parametrize("depth", [0, 1, 2, 3])
@pytest.mark.parametrize("where", ["object", "query"])
def test_credential_names(tmp_path, name, depth, where):
    directory = build(tmp_path)
    value = encoded(name, depth)
    field = {value: "dummy-sensitive-value"} if where == "object" else {
        "nested": ["https://example.test/?" + value + "=dummy-sensitive-value"]}
    mutate(directory / "run_started.json", extra=field)
    report = invalid(tmp_path, "credential_rejected")
    assert_safe(report, value, "dummy-sensitive-value")


@pytest.mark.parametrize("depth", [0, 1, 2, 3])
@pytest.mark.parametrize("where", ["value", "object_key", "path"])
def test_configured_key_encodings(tmp_path, depth, where):
    directory = build(tmp_path)
    value = encoded(KEY, depth)
    if where == "value":
        mutate(directory / "run_started.json", nested={"safe": [value]})
    elif where == "object_key":
        mutate(directory / "run_started.json", nested={value: "safe"})
    else:
        mutate(directory / "page_000000.json", relative_artifact_path=value + ".json")
    assert_safe(invalid(tmp_path, "credential_rejected"), value)


def test_form_encoded_credential(tmp_path):
    directory = build(tmp_path)
    mutate(directory / "run_started.json", extra=quote_plus(quote_plus(KEY)))
    invalid(tmp_path, "credential_rejected")


def test_key_none_name_checks_still_active(tmp_path):
    directory = build(tmp_path)
    report = verify(tmp_path, api_key=None)
    assert report.status == "complete" and not report.configured_key_check_complete
    assert "configured_key_unverified" in report.issue_categories
    mutate(directory / "run_started.json", extra={encoded("Authorization", 2): "anything"})
    assert verify(tmp_path, api_key=None).status == "invalid"


@pytest.mark.parametrize("run_id", ["", "../escape", "NUL", "COM1.txt", "trailing.", "a" * 129, None])
def test_invalid_run_id_before_filesystem(tmp_path, monkeypatch, run_id):
    def forbidden(*args, **kwargs):
        raise AssertionError("filesystem resolution before validation")
    monkeypatch.setattr(Path, "resolve", forbidden)
    report = verify_run(tmp_path, run_id, api_key=KEY)
    assert report.status == "invalid" and report.run_id is None
    assert "invalid_run_id" in report.issue_categories


@pytest.mark.parametrize("key", ["", " ", 1, "\ud800"])
def test_invalid_api_key_safe(tmp_path, key):
    report = verify_run(tmp_path, RUN_ID, api_key=key)
    assert report.status == "invalid" and "invalid_api_key" in report.issue_categories
    assert report.run_id is None


def test_secret_run_id_redacted(tmp_path):
    secret = "dummy-safe-id-secret"
    report = verify_run(tmp_path, secret, api_key=secret)
    assert report.status == "invalid" and report.run_id is None
    assert_safe(report, secret)


def test_read_failure_safe(tmp_path, monkeypatch):
    build(tmp_path)
    def denied(*args, **kwargs):
        raise PermissionError(KEY)
    monkeypatch.setattr(Path, "read_bytes", denied)
    assert_safe(invalid(tmp_path, "filesystem_error"))


def test_nonregular_journal_record(tmp_path):
    directory = build(tmp_path)
    path = directory / "page_000000.json"
    path.unlink()
    path.mkdir()
    invalid(tmp_path, "nonregular_entry")


def test_verifier_has_no_write_operations(tmp_path, monkeypatch):
    build(tmp_path)
    before = snapshot(tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("verification attempted filesystem mutation")
    with monkeypatch.context() as checks:
        for name in ("write_bytes", "write_text", "mkdir", "unlink", "rename", "replace", "rmdir"):
            checks.setattr(Path, name, forbidden)
        assert verify(tmp_path).status == "complete"
    assert snapshot(tmp_path) == before


def replace_artifact(root, record_path, **changes):
    """Tamper only a synthetic page while reconciling its storage hashes/names."""
    record = load(record_path)
    old_artifact = root / record["relative_artifact_path"]
    old_metadata = root / record["relative_metadata_path"]
    artifact = load(old_artifact)
    artifact.update(changes)
    content = (json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    digest = hashlib.sha256(content).hexdigest()
    stem = old_artifact.name[:-len(".sanitized.json")]
    stem = stem.replace(record["stored_artifact_sha256"][:12], digest[:12])
    new_artifact = old_artifact.with_name(stem + ".sanitized.json")
    new_metadata = old_metadata.with_name(stem + ".metadata.json")
    metadata = load(old_metadata)
    old_artifact.unlink()
    old_metadata.unlink()
    new_artifact.write_bytes(content)
    artifact_relative = new_artifact.relative_to(root).as_posix()
    metadata.update(stored_artifact_sha256=digest, stored_artifact_byte_count=len(content),
                    relative_stored_artifact_path=artifact_relative)
    new_metadata.write_text(json.dumps(metadata), encoding="utf-8")
    record.update(relative_artifact_path=artifact_relative,
                  relative_metadata_path=new_metadata.relative_to(root).as_posix(),
                  stored_artifact_sha256=digest)
    record_path.write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("changes", [
    {"type": "Feature"}, {"features": {}}, {"features": [None, None]},
    {"numberReturned": 1}, {"numberReturned": True}, {"numberMatched": True},
])
def test_artifact_structure_after_valid_hash(tmp_path, changes):
    directory = build(tmp_path)
    replace_artifact(tmp_path, directory / "page_000000.json", **changes)
    invalid(tmp_path, "invalid_source_structure")


def test_duplicate_ids_within_page(tmp_path):
    build(tmp_path, pages=[["same", "same"], ["tail"]])
    invalid(tmp_path, "duplicate_feature_ids")


def test_duplicate_ids_across_pages(tmp_path):
    build(tmp_path, pages=[["first", "second"], ["first"]])
    invalid(tmp_path, "duplicate_feature_ids")


def test_repeated_content_with_missing_ids(tmp_path):
    build(tmp_path, terminal=None, pages=[[None, None], [None, None]])
    invalid(tmp_path, "repeated_page_content")


def test_incomplete_ids_complete_summary(tmp_path):
    directory = build(tmp_path, pages=[[None]])
    report = verify(tmp_path)
    assert report.status == "complete"
    mutate(directory / "run_complete.json", duplicate_id_check_complete=True)
    invalid(tmp_path, "id_completeness_summary_mismatch")


@pytest.mark.parametrize("family", ["page", "terminal", "artifact", "metadata"])
def test_credential_scanning_all_document_families(tmp_path, family):
    directory = build(tmp_path)
    page = load(directory / "page_000000.json")
    path = {"page": directory / "page_000000.json", "terminal": directory / "run_complete.json",
            "artifact": tmp_path / page["relative_artifact_path"],
            "metadata": tmp_path / page["relative_metadata_path"]}[family]
    mutate(path, secret_nested=[{"safe": encoded(KEY, 3)}])
    invalid(tmp_path, "credential_rejected")


def test_safe_encoded_values_and_links(tmp_path):
    directory = build(tmp_path)
    mutate(directory / "run_started.json", extra={"safe": [
        "safe+form%2520text", "https://example.test/?safe%2520name=safe%2520value&limit=10",
        "api_key", "authorization", 12.5,
    ]})
    assert verify(tmp_path).status == "complete"


@pytest.mark.parametrize("filename", ["run_started.json", "page_000000.json", "run_complete.json"])
@pytest.mark.parametrize("content,issue", [
    (b"[]", "nonobject_document"), (b"{", "invalid_json"),
    (b'{"unsafe":Infinity}', "nonfinite_json_number"),
])
def test_invalid_journal_json(tmp_path, filename, content, issue):
    directory = build(tmp_path)
    (directory / filename).write_bytes(content)
    invalid(tmp_path, issue)


def test_failure_without_published_pages(tmp_path):
    build(tmp_path, terminal="failed", pages=[])
    report = verify(tmp_path)
    assert report.status == "failed" and report.journal_page_count == 0
    assert report.last_published_counts.to_dict() == {
        "attempted_request_count": 1, "persisted_page_count": 0,
        "validated_page_count": 0, "journaled_page_count": 0,
    }


def test_unvalidated_lineage_over_limit_is_not_reclassified(tmp_path):
    journal = RunJournal.start(output_root=tmp_path, run_id=RUN_ID, expected_offsets=(0, 2),
                               started_at=TIME, api_key=KEY, counts=RunCounts(0, 0, 0, 0))
    # This persisted response itself causes pagination validation failure.
    record = source(tmp_path, ids=["a", "b", "c"])
    reference = UnjournaledPageReference.from_page(record=record, outcome_stage="persisted_unvalidated",
                                                  output_root=tmp_path, api_key=KEY)
    journal.fail(failed_at=TIME, failure_category="number_returned_exceeds_limit",
                 counts=RunCounts(1, 1, 0, 0), unjournaled_page=reference)
    report = verify(tmp_path)
    assert report.status == "failed" and report.journal_page_count == 0
    assert report.last_published_counts.persisted_page_count == 1
    assert report.last_published_counts.validated_page_count == 0


def test_standalone_lineage_rejected(tmp_path):
    directory = build(tmp_path, coordinated=False, terminal="failed", pages=[])
    mutate(directory / "run_failed.json", unjournaled_page={})
    invalid(tmp_path, "invalid_unjournaled_lineage")


def test_lineage_extra_fields_rejected(tmp_path):
    directory = build(tmp_path, terminal="failed", pages=[], reference_stage="persisted_unvalidated")
    path = directory / "run_failed.json"
    terminal = load(path)
    terminal["unjournaled_page"]["unexpected"] = "unsafe-extra"
    path.write_text(json.dumps(terminal), encoding="utf-8")
    invalid(tmp_path, "invalid_unjournaled_lineage")


def test_counts_do_not_advance_past_invalid_terminal(tmp_path):
    directory = build(tmp_path, terminal="failed", pages=[["first", "second"]])
    mutate(directory / "run_failed.json", successful_page_count=99)
    report = invalid(tmp_path)
    assert report.last_published_counts.attempted_request_count == 1
    assert report.last_published_counts.journaled_page_count == 1
    assert not report.counts_final


def test_counts_do_not_advance_past_invalid_page(tmp_path):
    directory = build(tmp_path)
    mutate(directory / "page_000001.json", validated_page_count=0)
    report = invalid(tmp_path)
    assert report.journal_page_count == 1
    assert report.last_published_counts.attempted_request_count == 1
    assert not report.counts_final


def test_stat_failure_safe(tmp_path, monkeypatch):
    build(tmp_path)
    def denied(*args, **kwargs):
        raise PermissionError(KEY)
    monkeypatch.setattr(Path, "lstat", denied)
    invalid(tmp_path, "filesystem_error")


def test_resolve_failure_safe(tmp_path, monkeypatch):
    build(tmp_path)
    def denied(*args, **kwargs):
        raise OSError(KEY)
    monkeypatch.setattr(Path, "resolve", denied)
    invalid(tmp_path, "filesystem_error")


def test_no_terminal_or_lineage_inference_from_unrecorded_source(tmp_path):
    build(tmp_path, terminal=None, pages=[])
    source(tmp_path)  # Unreferenced artifact is not evidence of a journaled request.
    report = verify(tmp_path)
    assert report.status == "interrupted" and report.journal_page_count == 0
    assert report.last_published_counts.attempted_request_count == 0


def test_missing_required_metadata_field(tmp_path):
    directory = build(tmp_path)
    page = load(directory / "page_000000.json")
    path = tmp_path / page["relative_metadata_path"]
    value = load(path)
    del value["content_type"]
    path.write_text(json.dumps(value), encoding="utf-8")
    invalid(tmp_path, "invalid_provenance")


def test_completion_cannot_claim_unjournaled_lineage(tmp_path):
    directory = build(tmp_path)
    mutate(directory / "run_complete.json", unjournaled_page={})
    invalid(tmp_path, "invalid_unjournaled_lineage")


def test_process_interruption_propagates_without_changes(tmp_path, monkeypatch):
    build(tmp_path)
    before = snapshot(tmp_path)
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt()
    with monkeypatch.context() as checks:
        checks.setattr(Path, "read_bytes", interrupted)
        with pytest.raises(KeyboardInterrupt):
            verify(tmp_path)
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("damage", ["unexpected", "malformed", "bad_hash"])
def test_invalid_runs_still_read_only(tmp_path, damage):
    directory = build(tmp_path)
    if damage == "unexpected":
        (directory / "unexpected").mkdir()
    elif damage == "malformed":
        (directory / "run_complete.json").write_bytes(b'{')
    else:
        mutate(directory / "page_000000.json", stored_artifact_sha256="c" * 64)
    before = snapshot(tmp_path)
    invalid(tmp_path)
    assert snapshot(tmp_path) == before
