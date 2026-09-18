from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest
import requests

from src.ingestion import run_manifest
from src.ingestion.run_manifest import PageJournalRecord, RunJournal, RunJournalError


DUMMY_KEY = "dummy-journal-secret"
STARTED = datetime(2026, 8, 29, 1, 2, 3, tzinfo=timezone.utc)
FINISHED = STARTED + timedelta(minutes=2)
DIGEST = "a" * 64


@pytest.fixture(autouse=True)
def prohibit_external_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*_: object, **__: object) -> None:
        raise AssertionError("live network access is prohibited")

    def fail_dotenv(*_: object, **__: object) -> None:
        raise AssertionError("dotenv access is prohibited")

    monkeypatch.setattr(requests.sessions.Session, "request", fail_network)
    monkeypatch.setattr("src.configuration.dotenv_values", fail_dotenv)


def start(tmp_path: Path, *, run_id: str = "run-001", offsets=(0, 10)) -> RunJournal:
    return RunJournal.start(
        output_root=tmp_path,
        run_id=run_id,
        expected_offsets=offsets,
        started_at=STARTED,
        api_key=DUMMY_KEY,
    )


def page(tmp_path: Path, index: int = 0, offset: int = 0) -> PageJournalRecord:
    artifact = tmp_path / "gistda/flood_freq/pattani" / f"artifact-{index}.sanitized.json"
    metadata = tmp_path / "gistda/flood_freq/pattani" / f"artifact-{index}.metadata.json"
    return PageJournalRecord(
        page_index=index,
        requested_offset=offset,
        requested_limit=10,
        artifact_path=artifact,
        metadata_path=metadata,
        stored_artifact_sha256=DIGEST,
        number_returned=10 if index == 0 else 3,
        number_matched_present=True,
        number_matched=13,
        feature_id_check_complete=True,
    )


def read(path: Path) -> dict[str, object]:
    return json.loads(path.read_bytes())


def test_successful_start_pages_and_completion_are_immutable(tmp_path: Path) -> None:
    journal = start(tmp_path)
    first = journal.append_page(page(tmp_path))
    second = journal.append_page(page(tmp_path, 1, 10))
    terminal = journal.complete(
        completed_at=FINISHED,
        stop_reason="partial_page",
        observed_number_matched=13,
        duplicate_id_check_complete=True,
    )

    assert first.name == "page_000000.json"
    assert second.name == "page_000001.json"
    record = read(terminal)
    assert record["status"] == "complete"
    assert record["page_count"] == 2
    assert record["request_count"] is None
    assert record["journal_schema_version"] == "1.1"
    assert "unknown" in record["request_count_semantics"]
    assert record["total_features"] == 13
    assert record["started_at_utc"] == "2026-08-29T01:02:03Z"
    assert record["completed_at_utc"] == "2026-08-29T01:04:03Z"
    assert "not an official GISTDA contract" in record["pagination_policy_disclaimer"]
    assert {path.name for path in journal.run_directory.iterdir()} == {
        "run_started.json", "page_000000.json", "page_000001.json", "run_complete.json"
    }


def test_failed_terminal_contains_only_safe_failure_category(tmp_path: Path) -> None:
    journal = start(tmp_path)
    journal.append_page(page(tmp_path))
    terminal = journal.fail(failed_at=FINISHED, failure_category="http_failure")

    record = read(terminal)
    assert record["status"] == "failed"
    assert record["successful_page_count"] == 1
    assert record["last_successful_offset"] == 0
    assert "message" not in record and "response" not in record


def test_interrupted_run_has_no_terminal_file(tmp_path: Path) -> None:
    journal = start(tmp_path)
    journal.append_page(page(tmp_path))

    assert not (journal.run_directory / "run_complete.json").exists()
    assert not (journal.run_directory / "run_failed.json").exists()
    assert journal.is_terminal is False


def test_existing_run_directory_is_rejected(tmp_path: Path) -> None:
    start(tmp_path)
    with pytest.raises(FileExistsError):
        start(tmp_path)


@pytest.mark.parametrize("run_id", ["", ".", "..", "bad/name", "bad name", "../escape"])
def test_invalid_run_ids_are_rejected_without_directory(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError):
        start(tmp_path, run_id=run_id)
    assert not (tmp_path / "gistda/flood_freq/pattani/runs").exists()


def test_non_contiguous_page_index_is_rejected(tmp_path: Path) -> None:
    journal = start(tmp_path)
    with pytest.raises(RunJournalError, match="contiguous"):
        journal.append_page(page(tmp_path, 1, 10))


def test_offset_outside_supplied_sequence_is_rejected(tmp_path: Path) -> None:
    journal = start(tmp_path)
    with pytest.raises(RunJournalError, match="offset"):
        journal.append_page(page(tmp_path, 0, 10))


@pytest.mark.parametrize("field", ["artifact_path", "metadata_path"])
def test_external_artifact_or_metadata_paths_are_rejected(tmp_path: Path, field: str) -> None:
    journal = start(tmp_path)
    external = tmp_path.parent / "external.json"
    record = page(tmp_path)
    record = replace(record, **{field: external})
    with pytest.raises(ValueError, match="beneath"):
        journal.append_page(record)


def test_existing_journal_file_collision_is_not_overwritten(tmp_path: Path) -> None:
    journal = start(tmp_path)
    collision = journal.run_directory / "page_000000.json"
    collision.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        journal.append_page(page(tmp_path))
    assert collision.read_bytes() == b"existing"


def test_append_and_second_terminal_are_rejected_after_completion(tmp_path: Path) -> None:
    journal = start(tmp_path)
    journal.append_page(replace(page(tmp_path), number_returned=0, number_matched=0))
    journal.complete(completed_at=FINISHED, stop_reason="empty_page", observed_number_matched=0, duplicate_id_check_complete=True)
    with pytest.raises(RunJournalError, match="final"):
        journal.append_page(page(tmp_path))
    with pytest.raises(RunJournalError, match="final"):
        journal.fail(failed_at=FINISHED, failure_category="later_failure")


def test_atomic_publication_failure_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = start(tmp_path)

    def fail_link(*_: object, **__: object) -> None:
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(run_manifest.os, "link", fail_link)
    with pytest.raises(RunJournalError, match="publication_failed"):
        journal.append_page(page(tmp_path))
    assert not (journal.run_directory / "page_000000.json").exists()
    assert not list(journal.run_directory.glob("*.tmp"))
    assert not list(journal.run_directory.glob(".*.tmp"))


def test_secret_is_absent_from_repr_and_all_records(tmp_path: Path) -> None:
    journal = start(tmp_path)
    journal.append_page(page(tmp_path))
    journal.fail(failed_at=FINISHED, failure_category="safe_failure")

    assert DUMMY_KEY not in repr(journal)
    for path in journal.run_directory.iterdir():
        content = path.read_bytes()
        assert DUMMY_KEY.encode() not in content
        assert b"API-Key" not in content
        assert b"api_key" not in content


def test_invalid_failure_category_cannot_capture_exception_text(tmp_path: Path) -> None:
    journal = start(tmp_path)
    with pytest.raises(ValueError):
        journal.fail(failed_at=FINISHED, failure_category="timeout: secret detail")
    assert not (journal.run_directory / "run_failed.json").exists()


def finish(journal: RunJournal, **changes):
    values = dict(completed_at=FINISHED, stop_reason="partial_page",
                  observed_number_matched=13, duplicate_id_check_complete=True)
    return journal.complete(**(values | changes))


def snapshot(directory: Path):
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}


@pytest.mark.parametrize("contents", [False, True])
def test_existing_empty_or_nonempty_directory_is_preserved(tmp_path, contents):
    directory = tmp_path / run_manifest.RUNS_SUBDIRECTORY / "run-001"
    directory.mkdir(parents=True)
    if contents:
        (directory / ".run_started.json.other.tmp").write_bytes(b"another writer")
        (directory / "run_complete.json").write_bytes(b"existing immutable record")
    before = snapshot(directory)
    with pytest.raises(FileExistsError):
        start(tmp_path)
    assert directory.is_dir()
    assert snapshot(directory) == before


@pytest.mark.parametrize("run_id", ["CON", "nul.json", "COM1", "LPT9.data", "trailing.", "a" * 129, None])
def test_windows_unsafe_run_ids_are_rejected(tmp_path, run_id):
    with pytest.raises(ValueError):
        start(tmp_path, run_id=run_id)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("offsets", [(), (10,), (False,), (0, True), (0, 0), (0, 10, 5), (0, -1), (0, "10"), (0, 10.0), None])
def test_invalid_expected_offsets_fail_before_creation(tmp_path, offsets):
    with pytest.raises(ValueError):
        start(tmp_path, offsets=offsets)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("policy", ["", "Bad", "bad policy", "../policy", None])
def test_invalid_policy_fails_before_creation(tmp_path, policy):
    with pytest.raises(ValueError):
        RunJournal.start(output_root=tmp_path, run_id="run", expected_offsets=[0],
                         started_at=STARTED, api_key=DUMMY_KEY, pagination_policy=policy)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field,value", [
    ("page_index", True), ("page_index", 0.0), ("page_index", -1), ("page_index", "0"),
    ("requested_offset", False), ("requested_offset", -1), ("requested_offset", 0.0),
    ("requested_offset", "0"),
    ("requested_limit", 0), ("requested_limit", 10001), ("requested_limit", -1),
    ("requested_limit", True), ("requested_limit", 10.0), ("requested_limit", "10"),
    ("number_returned", -1), ("number_returned", 11), ("number_returned", True),
    ("number_returned", 1.0), ("number_returned", "1"),
    ("number_matched_present", 1), ("number_matched", True), ("number_matched", -1),
    ("number_matched", None), ("number_matched", "13"), ("number_matched", 13.0),
    ("feature_id_check_complete", 1), ("stored_artifact_sha256", "not-a-digest"),
])
def test_invalid_page_values_do_not_publish_or_advance(tmp_path, field, value):
    journal = start(tmp_path)
    before = snapshot(journal.run_directory)
    with pytest.raises(ValueError):
        journal.append_page(replace(page(tmp_path), **{field: value}))
    assert journal.page_count == 0
    assert snapshot(journal.run_directory) == before


@pytest.mark.parametrize("limit", [1, 10000])
def test_limit_bounds_and_empty_page_are_accepted(tmp_path, limit):
    journal = start(tmp_path)
    journal.append_page(replace(page(tmp_path), requested_limit=limit, number_returned=0))
    finish(journal, stop_reason="empty_page")
    assert journal.status == "complete"


def test_offset_recurrence_is_checked_independently_of_sequence(tmp_path):
    journal = start(tmp_path, offsets=(0, 20))
    journal.append_page(page(tmp_path))
    with pytest.raises(RunJournalError, match="progression"):
        journal.append_page(page(tmp_path, 1, 20))
    assert journal.page_count == 1


def test_varying_limits_follow_previous_limit(tmp_path):
    journal = start(tmp_path, offsets=(0, 2, 5))
    journal.append_page(replace(page(tmp_path), requested_limit=2, number_returned=2))
    journal.append_page(replace(page(tmp_path, 1, 2), requested_limit=3, number_returned=3))
    journal.append_page(replace(page(tmp_path, 2, 5), requested_limit=4, number_returned=1))
    terminal = finish(journal)
    assert read(terminal)["total_features"] == 6  # No equality-to-numberMatched assumption.


@pytest.mark.parametrize("count", [0, 3])
def test_page_after_empty_or_partial_is_rejected(tmp_path, count):
    journal = start(tmp_path)
    journal.append_page(replace(page(tmp_path), number_returned=count))
    before = snapshot(journal.run_directory)
    with pytest.raises(RunJournalError, match="stop_page"):
        journal.append_page(page(tmp_path, 1, 10))
    assert journal.page_count == 1
    assert snapshot(journal.run_directory) == before


@pytest.mark.parametrize("first,second", [
    ((True, 13), (True, 14)), ((True, 13), (False, None)), ((False, None), (True, 13)),
])
def test_number_matched_changes_are_rejected(tmp_path, first, second):
    journal = start(tmp_path)
    journal.append_page(replace(page(tmp_path), number_matched_present=first[0], number_matched=first[1]))
    with pytest.raises(RunJournalError, match="number_matched_changed"):
        journal.append_page(replace(page(tmp_path, 1, 10), number_matched_present=second[0], number_matched=second[1]))
    assert journal.page_count == 1


def test_consistently_absent_number_matched_completes_as_null(tmp_path):
    journal = start(tmp_path)
    for index, offset in enumerate((0, 10)):
        journal.append_page(replace(page(tmp_path, index, offset), number_matched_present=False, number_matched=None))
    terminal = finish(journal, observed_number_matched=None)
    assert read(terminal)["observed_number_matched"] is None
    assert read(terminal)["observed_number_matched_present"] is False


@pytest.mark.parametrize("changes", [
    {"completed_at": STARTED - timedelta(seconds=1)},
    {"completed_at": STARTED.replace(tzinfo=None)}, {"completed_at": "bad"},
    {"stop_reason": "bad reason"}, {"stop_reason": None}, {"stop_reason": "unknown_reason"},
    {"observed_number_matched": 14}, {"observed_number_matched": None},
    {"observed_number_matched": True}, {"observed_number_matched": -1},
    {"observed_number_matched": 13.0}, {"observed_number_matched": "13"},
    {"duplicate_id_check_complete": 1},
])
def test_invalid_completion_leaves_open_disk_and_memory_unchanged(tmp_path, changes):
    journal = start(tmp_path)
    journal.append_page(replace(page(tmp_path), number_returned=3))
    before = snapshot(journal.run_directory)
    with pytest.raises((ValueError, RunJournalError)):
        finish(journal, **changes)
    assert journal.status == "started" and not journal.is_terminal
    assert snapshot(journal.run_directory) == before


@pytest.mark.parametrize("count,reason", [(0, "partial_page"), (3, "empty_page"), (10, "partial_page"), (10, "empty_page")])
def test_stop_reason_matches_final_count(tmp_path, count, reason):
    journal = start(tmp_path)
    journal.append_page(replace(page(tmp_path), number_returned=count))
    with pytest.raises(RunJournalError, match="stop_reason"):
        finish(journal, stop_reason=reason)


def test_completion_requires_a_page_but_failure_does_not(tmp_path):
    journal = start(tmp_path)
    with pytest.raises(RunJournalError, match="requires_page"):
        finish(journal)
    terminal = journal.fail(failed_at=STARTED, failure_category="request_failure")
    assert read(terminal)["successful_page_count"] == 0
    assert read(terminal)["last_successful_offset"] is None
    assert read(terminal)["request_count"] is None


def test_incomplete_ids_cannot_be_reported_complete(tmp_path):
    journal = start(tmp_path)
    journal.append_page(replace(page(tmp_path), number_returned=3, feature_id_check_complete=False))
    with pytest.raises(RunJournalError, match="id_completeness"):
        finish(journal)
    assert not journal.is_terminal
    terminal = finish(journal, duplicate_id_check_complete=False)
    assert read(terminal)["duplicate_id_check_complete"] is False


@pytest.mark.parametrize("terminal", ["run_complete.json", "run_failed.json"])
def test_existing_terminal_prevents_any_operation(tmp_path, terminal):
    journal = start(tmp_path)
    (journal.run_directory / terminal).write_bytes(b"existing")
    with pytest.raises(RunJournalError, match="terminal_record"):
        journal.append_page(page(tmp_path))
    with pytest.raises(RunJournalError, match="terminal_record"):
        journal.fail(failed_at=FINISHED, failure_category="later_failure")
    assert (journal.run_directory / terminal).read_bytes() == b"existing"


@pytest.mark.parametrize("stage", ["write", "flush", "fsync", "close", "creation"])
def test_temp_setup_failure_cleans_files_and_preserves_safe_original_category(tmp_path, monkeypatch, stage):
    journal = start(tmp_path)
    original_factory = run_manifest.tempfile.NamedTemporaryFile
    unsafe = OSError(f"synthetic detail {DUMMY_KEY}")

    class BrokenTemporary:
        def __init__(self, underlying):
            self.underlying = underlying
            self.name = underlying.name
            self.close_calls = 0

        def write(self, content):
            if stage == "write":
                raise unsafe
            return self.underlying.write(content)

        def flush(self):
            if stage == "flush":
                raise unsafe
            return self.underlying.flush()

        def fileno(self):
            return self.underlying.fileno()

        def close(self):
            self.close_calls += 1
            if stage == "close" and self.close_calls == 1:
                raise unsafe
            return self.underlying.close()

    def factory(**kwargs):
        if stage == "creation":
            raise unsafe
        return BrokenTemporary(original_factory(**kwargs))

    monkeypatch.setattr(run_manifest.tempfile, "NamedTemporaryFile", factory)
    if stage == "fsync":
        def fail_sync(_):
            raise unsafe
        monkeypatch.setattr(run_manifest.os, "fsync", fail_sync)
    before = snapshot(journal.run_directory)
    with pytest.raises(RunJournalError) as caught:
        journal.append_page(page(tmp_path))
    assert caught.value.failure_category == "temporary_write_failed"
    assert not caught.value.record_published and not caught.value.cleanup_failed
    assert caught.value.__suppress_context__
    assert DUMMY_KEY not in str(caught.value)
    assert snapshot(journal.run_directory) == before
    assert journal.page_count == 0


def test_failed_start_removes_only_owned_empty_run_directory(tmp_path, monkeypatch):
    def fail_link(*_):
        raise OSError("synthetic")
    monkeypatch.setattr(run_manifest.os, "link", fail_link)
    with pytest.raises(RunJournalError):
        start(tmp_path)
    assert not (tmp_path / run_manifest.RUNS_SUBDIRECTORY / "run-001").exists()


def test_destination_appearing_at_publication_is_preserved(tmp_path, monkeypatch):
    journal = start(tmp_path)
    real_link = os.link
    def race(source, target):
        Path(target).write_bytes(b"other writer record")
        real_link(source, target)
    monkeypatch.setattr(run_manifest.os, "link", race)
    with pytest.raises(FileExistsError):
        journal.append_page(page(tmp_path))
    assert (journal.run_directory / "page_000000.json").read_bytes() == b"other writer record"
    assert not list(journal.run_directory.glob("*.tmp"))
    assert journal.page_count == 0


@pytest.mark.parametrize("kind", ["page", "complete", "failed"])
def test_published_record_with_cleanup_fault_updates_state_and_blocks_instance(tmp_path, monkeypatch, kind):
    journal = start(tmp_path)
    if kind == "complete":
        journal.append_page(replace(page(tmp_path), number_returned=3))
    real_unlink = Path.unlink
    def fail_temp_unlink(path, *args, **kwargs):
        if path.name.endswith(".tmp"):
            raise OSError(f"unsafe cleanup detail {DUMMY_KEY}")
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_temp_unlink)
    with pytest.raises(RunJournalError) as caught:
        if kind == "page":
            journal.append_page(page(tmp_path))
        elif kind == "complete":
            finish(journal)
        else:
            journal.fail(failed_at=FINISHED, failure_category="request_failure")
    assert caught.value.record_published is True
    assert caught.value.cleanup_failed is True
    assert journal.cleanup_failed is True
    assert DUMMY_KEY not in str(caught.value)
    if kind == "page":
        assert journal.page_count == 1
        assert not journal.is_terminal
        assert (journal.run_directory / "page_000000.json").exists()
    else:
        assert journal.is_terminal
        assert journal.status == kind
        filename = "run_complete.json" if kind == "complete" else "run_failed.json"
        assert read(journal.run_directory / filename)["status"] == kind
    before = snapshot(journal.run_directory)
    for operation in (lambda: journal.append_page(page(tmp_path, 1, 10)),
                      lambda: finish(journal),
                      lambda: journal.fail(failed_at=FINISHED, failure_category="later_failure")):
        with pytest.raises(RunJournalError):
            operation()
    assert snapshot(journal.run_directory) == before
    if kind == "complete":
        assert not (journal.run_directory / "run_failed.json").exists()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    for temporary in journal.run_directory.glob("*.tmp"):
        temporary.unlink()


@pytest.mark.parametrize("secret", ['dummy-"quoted"-key', 'dummy-\\backslash-key', 'dummy-\ncontrol-key', 'dummy-encoded/key'])
@pytest.mark.parametrize("encoded", [False, True])
def test_parsed_and_encoded_secret_is_rejected_before_persistence(tmp_path, secret, encoded):
    journal = RunJournal.start(output_root=tmp_path, run_id="safe", expected_offsets=[0],
                               started_at=STARTED, api_key=secret)
    leaked = quote(secret, safe="") if encoded else secret
    record = replace(page(tmp_path), artifact_path=tmp_path / f"prefix-{leaked}.json")
    before = snapshot(journal.run_directory)
    with pytest.raises(RunJournalError) as caught:
        journal.append_page(record)
    assert secret not in str(caught.value) and leaked not in str(caught.value)
    assert secret not in repr(record) and secret not in repr(journal)
    assert snapshot(journal.run_directory) == before


@pytest.mark.parametrize("key", ["api_key", "API-Key", "Authorization", "%61pi_key"])
def test_credential_fields_are_rejected_by_serializer(key):
    with pytest.raises(RunJournalError):
        run_manifest._serialize_and_verify({"nested": [{key: "dummy-value"}]}, DUMMY_KEY)


@pytest.mark.parametrize("text", ["https://example.test/?%61pi_key=dummy", "API-Key: dummy", "Authorization: dummy"])
def test_credential_query_and_header_forms_are_rejected(text):
    with pytest.raises(RunJournalError):
        run_manifest._serialize_and_verify({"safe": text}, DUMMY_KEY)


def test_injected_timestamp_normalizes_to_utc(tmp_path):
    local = STARTED.astimezone(timezone(timedelta(hours=7)))
    journal = RunJournal.start(output_root=tmp_path, run_id="run", expected_offsets=[0],
                               started_at=local, api_key=DUMMY_KEY)
    terminal = journal.fail(failed_at=local, failure_category="safe_failure")
    assert read(terminal)["failed_at_utc"] == "2026-08-29T01:02:03Z"


@pytest.mark.parametrize("key", ["", "   ", None])
def test_missing_check_credential_fails_before_creation(tmp_path, key):
    with pytest.raises(ValueError):
        RunJournal.start(output_root=tmp_path, run_id="run", expected_offsets=[0],
                         started_at=STARTED, api_key=key)
    assert list(tmp_path.iterdir()) == []


def test_secret_in_start_record_fails_before_creation(tmp_path):
    with pytest.raises(RunJournalError) as caught:
        start(tmp_path, run_id=DUMMY_KEY)
    assert DUMMY_KEY not in str(caught.value)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("timestamp", [STARTED.replace(tzinfo=None), "invalid", None])
def test_invalid_start_timestamp_fails_before_creation(tmp_path, timestamp):
    with pytest.raises(ValueError):
        RunJournal.start(output_root=tmp_path, run_id="run", expected_offsets=[0],
                         started_at=timestamp, api_key=DUMMY_KEY)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("changes", [
    {"failed_at": STARTED - timedelta(seconds=1)},
    {"failed_at": STARTED.replace(tzinfo=None)}, {"failed_at": "invalid"},
    {"failure_category": "bad category"}, {"failure_category": None},
    {"failure_category": DUMMY_KEY},
])
def test_failed_terminal_validation_preserves_open_journal(tmp_path, changes):
    journal = start(tmp_path)
    before = snapshot(journal.run_directory)
    values = dict(failed_at=FINISHED, failure_category="safe_failure") | changes
    with pytest.raises((ValueError, RunJournalError)) as caught:
        journal.fail(**values)
    assert DUMMY_KEY not in str(caught.value)
    assert not journal.is_terminal
    assert snapshot(journal.run_directory) == before


def test_append_and_completion_after_failed_terminal_are_rejected(tmp_path):
    journal = start(tmp_path)
    journal.fail(failed_at=FINISHED, failure_category="safe_failure")
    before = snapshot(journal.run_directory)
    for operation in (lambda: journal.append_page(page(tmp_path)), lambda: finish(journal),
                      lambda: journal.fail(failed_at=FINISHED, failure_category="later_failure")):
        with pytest.raises(RunJournalError, match="final"):
            operation()
    assert journal.status == "failed" and journal.is_terminal
    assert snapshot(journal.run_directory) == before


def test_supplied_sequence_exhaustion_does_not_publish(tmp_path):
    journal = start(tmp_path, offsets=(0,))
    journal.append_page(page(tmp_path))
    with pytest.raises(RunJournalError, match="sequence_exhausted"):
        journal.append_page(page(tmp_path, 1, 10))
    assert journal.page_count == 1


def test_absent_number_matched_with_value_is_rejected(tmp_path):
    journal = start(tmp_path)
    with pytest.raises(ValueError, match="absent"):
        journal.append_page(replace(page(tmp_path), number_matched_present=False))
    assert journal.page_count == 0


def test_conservative_incomplete_summary_is_allowed(tmp_path):
    journal = start(tmp_path)
    journal.append_page(replace(page(tmp_path), number_returned=3))
    terminal = finish(journal, duplicate_id_check_complete=False)
    assert read(terminal)["duplicate_id_check_complete"] is False


@pytest.mark.parametrize("stage", ["setup", "publication"])
def test_original_failure_category_survives_cleanup_failure(tmp_path, monkeypatch, stage):
    journal = start(tmp_path)
    real_unlink = Path.unlink
    def refuse_removal(path, *args, **kwargs):
        if path.name.endswith(".tmp"):
            raise OSError("synthetic cleanup refusal")
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", refuse_removal)
    def fail(*_):
        raise OSError(f"unsafe original detail {DUMMY_KEY}")
    monkeypatch.setattr(run_manifest.os, "fsync" if stage == "setup" else "link", fail)
    with pytest.raises(RunJournalError) as caught:
        journal.append_page(page(tmp_path))
    expected = "temporary_write_failed" if stage == "setup" else "publication_failed"
    assert caught.value.failure_category == expected
    assert caught.value.cleanup_failed and not caught.value.record_published
    assert journal.cleanup_failed and journal.page_count == 0
    assert not (journal.run_directory / "page_000000.json").exists()
    assert DUMMY_KEY not in str(caught.value)
    with pytest.raises(RunJournalError, match="blocked"):
        journal.fail(failed_at=FINISHED, failure_category="safe_failure")
    monkeypatch.setattr(Path, "unlink", real_unlink)
    for temporary in journal.run_directory.glob("*.tmp"):
        temporary.unlink()


def test_start_publication_cleanup_failure_keeps_started_disk_record(tmp_path, monkeypatch):
    real_unlink = Path.unlink
    def refuse_removal(path, *args, **kwargs):
        if path.name.endswith(".tmp"):
            raise OSError("synthetic cleanup refusal")
        return real_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", refuse_removal)
    with pytest.raises(RunJournalError) as caught:
        start(tmp_path)
    assert caught.value.record_published and caught.value.cleanup_failed
    directory = tmp_path / run_manifest.RUNS_SUBDIRECTORY / "run-001"
    assert read(directory / "run_started.json")["status"] == "started"
    assert not (directory / "run_complete.json").exists()
    assert not (directory / "run_failed.json").exists()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    for temporary in directory.glob("*.tmp"):
        temporary.unlink()


def test_directory_creation_race_preserves_other_run(tmp_path, monkeypatch):
    real_mkdir = Path.mkdir
    directory = tmp_path / run_manifest.RUNS_SUBDIRECTORY / "run-001"
    def race(path, *args, **kwargs):
        if path == directory:
            real_mkdir(path, parents=True, exist_ok=True)
            (path / ".other.tmp").write_bytes(b"other operation")
        return real_mkdir(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", race)
    with pytest.raises(FileExistsError):
        start(tmp_path)
    assert (directory / ".other.tmp").read_bytes() == b"other operation"


def test_resolved_run_path_escape_is_rejected_before_creation(tmp_path, monkeypatch):
    real_resolve = Path.resolve
    target = tmp_path / run_manifest.RUNS_SUBDIRECTORY / "run-001"
    def escape(path, *args, **kwargs):
        if path == target:
            return tmp_path.parent / "external-run"
        return real_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", escape)
    with pytest.raises(ValueError, match="beneath"):
        start(tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["artifact_path", "metadata_path"])
def test_resolved_page_path_escape_is_rejected(tmp_path, monkeypatch, field):
    journal = start(tmp_path)
    real_resolve = Path.resolve
    redirected = tmp_path / "redirected.json"
    def escape(path, *args, **kwargs):
        if path == redirected:
            return tmp_path.parent / "external.json"
        return real_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", escape)
    with pytest.raises(ValueError, match="beneath"):
        journal.append_page(replace(page(tmp_path), **{field: redirected}))
    assert journal.page_count == 0


@pytest.mark.parametrize("encoded", [False, True])
def test_nested_json_escaped_and_form_encoded_secret_is_rejected(encoded):
    secret = 'dummy "quoted"\\key with space'
    value = quote_plus(quote_plus(secret)) if encoded else secret
    with pytest.raises(RunJournalError) as caught:
        run_manifest._serialize_and_verify({"nested": [{"safe": value}]}, secret)
    assert secret not in str(caught.value) and value not in str(caught.value)


def test_single_writer_limitation_is_explicit(tmp_path):
    journal = start(tmp_path)
    assert read(journal.run_directory / "run_started.json")["writer_model"] == "single_writer_per_run"


def encoded_name(name: str, depth: int) -> str:
    # Encode every character first, including characters quote() leaves safe.
    if depth == 0:
        return name
    value = "".join(f"%{ord(character):02X}" for character in name)
    for _ in range(depth - 1):
        value = quote_plus(value)
    return value


@pytest.mark.parametrize("name", ["api_key", "API-Key", "Authorization"])
@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_encoded_credential_object_names_reject_new_record(
    tmp_path, monkeypatch, name, depth
):
    journal = start(tmp_path)
    offending_name = encoded_name(name, depth)
    offending_value = "dummy-field-secret-value"
    base_record = journal._base_record()
    monkeypatch.setattr(journal, "_base_record", lambda: base_record | {
        "nested": [{offending_name: offending_value}]
    })
    before = snapshot(journal.run_directory)
    record = page(tmp_path)

    with pytest.raises(RunJournalError) as caught:
        journal.append_page(record)

    assert caught.value.failure_category == "credential_field_rejected"
    for sensitive in (DUMMY_KEY, offending_name, offending_value, name):
        assert sensitive not in str(caught.value)
        assert sensitive not in repr(caught.value)
        assert sensitive not in repr(journal)
        assert sensitive not in repr(record)
    assert not caught.value.record_published
    assert journal.page_count == 0 and not journal.is_terminal
    assert snapshot(journal.run_directory) == before


@pytest.mark.parametrize("name", ["api_key", "API-Key", "authorization"])
@pytest.mark.parametrize("depth", [0, 1, 2, 3])
def test_encoded_credential_query_names_reject_new_record(
    tmp_path, monkeypatch, name, depth
):
    journal = start(tmp_path)
    offending_name = encoded_name(name, depth)
    offending_value = "dummy-query-secret-value"
    url = f"https://example.test/?safe=preserved&{offending_name}={offending_value}"
    base_record = journal._base_record()
    monkeypatch.setattr(journal, "_base_record", lambda: base_record | {
        "nested": [{"safe": url}]
    })
    before = snapshot(journal.run_directory)
    record = page(tmp_path)

    with pytest.raises(RunJournalError) as caught:
        journal.append_page(record)

    assert caught.value.failure_category == "credential_query_rejected"
    for sensitive in (DUMMY_KEY, offending_name, offending_value, name, url):
        assert sensitive not in str(caught.value)
        assert sensitive not in repr(caught.value)
        assert sensitive not in repr(journal)
        assert sensitive not in repr(record)
    assert not caught.value.record_published
    assert journal.page_count == 0 and not journal.is_terminal
    assert snapshot(journal.run_directory) == before


@pytest.mark.parametrize("name", ["%2561pi_key", "%41uthorization", "%2541uthorization"])
def test_partially_encoded_object_names_are_rejected(name):
    with pytest.raises(RunJournalError, match="credential_field_rejected") as caught:
        run_manifest._serialize_and_verify({"nested": [{name: DUMMY_KEY}]}, DUMMY_KEY)
    assert name not in str(caught.value) and DUMMY_KEY not in str(caught.value)


def test_form_decoding_of_credential_names_reaches_stability():
    assert run_manifest._normalized_credential_name("%252BAPI-Key") == " api-key"
    assert run_manifest._normalized_credential_name("%2541uthorization") == "authorization"
