from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
import requests

from src.configuration import GistdaConfig, OFFICIAL_GISTDA_API_BASE_URL
from src.ingestion.flood_frequency import (
    SourcePageStructureError,
    SourceSanitizationError,
)
from src.ingestion.gistda_client import (
    GistdaConnectionError, GistdaHTTPError, GistdaResponse, GistdaTimeoutError,
)
from src.ingestion import pagination, run_manifest
from src.ingestion.pagination import PaginationPolicyError, PaginationRunError, paginate_pattani
from src.ingestion.run_manifest import RunCounts, RunJournal, RunJournalError
from tests.test_flood_frequency import DUMMY_KEY, FIXED_TIME


NUMBER_MATCHED_ABSENT = object()


def page_bytes(
    ids: list[str | None],
    *,
    number_returned: object | None = None,
    number_matched: object = 5,
    next_offset: int = 999999,
) -> bytes:
    features = [
        ({"type": "Feature", "properties": {}} if value is None else {"id": value, "type": "Feature", "properties": {}})
        for value in ids
    ]
    payload = {
        "type": "FeatureCollection",
        "features": features,
        "numberReturned": len(features) if number_returned is None else number_returned,
        "links": [
            {
                "rel": "next",
                "href": (
                    "https://api.example.test/features/flood-freq"
                    f"?api_key={DUMMY_KEY}&offset={next_offset}&limit=1"
                ),
            }
        ],
    }
    if number_matched is not NUMBER_MATCHED_ABSENT:
        payload["numberMatched"] = number_matched
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


class FaultClient:
    """Synthetic steps; never construct a requests session."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def get_flood_frequency(self, **kwargs):
        self.calls.append(kwargs)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return GistdaResponse(step, 200, "application/geo+json")


def journaled(tmp_path, client, *, max_pages=10, run_id="integrated", clock=None):
    return paginate_pattani(config=config(), client=client, output_root=tmp_path,
                            limit=2, max_pages=max_pages, retrieved_at=FIXED_TIME,
                            run_id=run_id, clock=clock or (lambda: FIXED_TIME))


def journal_directory(tmp_path):
    return tmp_path / run_manifest.RUNS_SUBDIRECTORY / "integrated"


def assert_safe_outcome(error):
    assert DUMMY_KEY not in str(error) + repr(error) + repr(error.counts)
    assert error.__cause__ is None and error.__context__ is None
    assert not hasattr(error, "original_exception")


def test_journaled_full_pages_then_partial_completion(tmp_path):
    client = FaultClient([page_bytes(["a", "b"]), page_bytes(["c", "d"]), page_bytes(["e"])])
    result = journaled(tmp_path, client)
    assert result.requested_offsets == (0, 2, 4)
    assert [call["offset"] for call in client.calls] == [0, 2, 4]
    assert all(call == {"pv_idn": "94", "limit": 2, "offset": i * 2}
               for i, call in enumerate(client.calls))
    assert result.counts == RunCounts(3, 3, 3, 3)
    assert result.stop_reason == "partial_page" and result.total_features == 5
    terminal = json.loads(result.terminal_path.read_bytes())
    assert terminal["request_count"] == terminal["page_count"] == 3
    assert terminal["status"] == "complete" and terminal["journal_schema_version"] == "1.2"
    assert result.to_dict()["counts"] == result.counts.to_dict()
    assert_artifacts_are_credential_safe(result)
    for record in result.run_directory.glob("*.json"):
        assert DUMMY_KEY.encode() not in record.read_bytes()
    assert "feature_ids" not in repr(result)


@pytest.mark.parametrize("steps,offsets", [
    ([page_bytes([], number_matched=0)], (0,)),
    ([page_bytes(["a", "b"], number_matched=2), page_bytes([], number_matched=2)], (0, 2)),
])
def test_journaled_empty_stop_pages(tmp_path, steps, offsets):
    result = journaled(tmp_path, FaultClient(steps))
    assert result.stop_reason == "empty_page" and result.requested_offsets == offsets
    assert result.counts == RunCounts(*([len(offsets)] * 4))


def test_journal_cap_boundary_accepts_immediate_stop(tmp_path):
    client = FaultClient([page_bytes([], number_matched=0)])
    result = journaled(tmp_path, client, max_pages=100_000)
    started = json.loads((result.run_directory / "run_started.json").read_bytes())
    assert len(started["expected_offsets"]) == 100_000
    assert started["expected_offsets"][-1] == 199_998
    assert len(client.calls) == 1


def test_journal_cap_rejected_before_allocation_clock_disk_dispatch(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("allocation or lifecycle evaluation occurred")
    monkeypatch.setattr(pagination, "range", forbidden, raising=False)
    client = FaultClient([])
    with pytest.raises(ValueError, match="allocation policy"):
        journaled(tmp_path, client, max_pages=100_001, clock=forbidden)
    assert client.calls == [] and list(tmp_path.iterdir()) == []


def test_legacy_shape_exceptions_and_uncapped_behavior(tmp_path):
    def forbidden():
        raise AssertionError("legacy lifecycle clock must not be evaluated")
    result = paginate_pattani(config=config(), client=FaultClient([page_bytes([])]),
                              output_root=tmp_path, limit=2, max_pages=100_001,
                              retrieved_at=FIXED_TIME, clock=forbidden)
    assert result.counts is None and result.run_id is None
    assert set(result.to_dict()) == {
        "requested_offsets", "stop_reason", "page_count", "total_features",
        "observed_number_matched", "duplicate_id_check_complete",
        "missing_feature_id_count", "pages",
    }
    assert not (tmp_path / run_manifest.RUNS_SUBDIRECTORY).exists()
    error = GistdaTimeoutError()
    with pytest.raises(GistdaTimeoutError) as caught:
        paginate(tmp_path, FaultClient([error]))
    assert caught.value is error


def test_journal_created_before_dispatch_and_injected_lifecycle_clock(tmp_path):
    events = []
    def clock():
        events.append("clock")
        return FIXED_TIME
    class OrderedClient(FaultClient):
        def get_flood_frequency(self, **kwargs):
            assert (journal_directory(tmp_path) / "run_started.json").exists()
            events.append("dispatch")
            return super().get_flood_frequency(**kwargs)
    result = journaled(tmp_path, OrderedClient([page_bytes([])]), clock=clock)
    assert events == ["clock", "dispatch", "clock"]
    terminal = json.loads(result.terminal_path.read_bytes())
    assert terminal["started_at_utc"] == terminal["completed_at_utc"]


@pytest.mark.parametrize("exception,category", [
    (GistdaTimeoutError(), "request_timeout"),
    (GistdaConnectionError(), "request_connection_failed"),
    (GistdaHTTPError(403, None), "request_http_failed"),
    (RuntimeError(DUMMY_KEY), "unexpected_failure"),
])
def test_request_failures_safe_counted_once_and_stop(tmp_path, exception, category):
    client = FaultClient([exception, page_bytes([])])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client)
    error = caught.value
    assert error.failure_category == category and error.run_status == "failed"
    assert error.counts == RunCounts(1, 0, 0, 0)
    assert len(client.calls) == 1
    assert not list(tmp_path.rglob("*.sanitized.json"))
    assert error.unjournaled_page is None
    terminal = json.loads(error.terminal_path.read_bytes())
    assert terminal["request_count"] == 1 and terminal["successful_page_count"] == 0
    assert_safe_outcome(error)


@pytest.mark.parametrize("body,category", [
    (b"not-json", "source_sanitization_failed"),
    (b'{"features": [], "numberReturned": true}', "source_structure_invalid"),
    (json.dumps({"nested": {"unsafe": DUMMY_KEY}, "features": [], "numberReturned": 0}).encode(),
     "source_sanitization_failed"),
])
def test_source_prepublication_failure_creates_only_failed_journal(tmp_path, body, category):
    client = FaultClient([body, page_bytes([])])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client)
    assert caught.value.failure_category == category
    assert caught.value.counts == RunCounts(1, 0, 0, 0)
    assert caught.value.run_status == "failed" and len(client.calls) == 1
    assert not list(tmp_path.rglob("*.sanitized.json"))
    assert caught.value.unjournaled_page is None
    assert_safe_outcome(caught.value)


@pytest.mark.parametrize("steps,category,counts", [
    ([page_bytes(["a", "b"]), page_bytes(["a", "b"])], "repeated_page", RunCounts(2, 2, 1, 1)),
    ([page_bytes(["a", "b"]), page_bytes(["b"])], "duplicate_feature_id", RunCounts(2, 2, 1, 1)),
    ([page_bytes(["a", "a"])], "duplicate_feature_id", RunCounts(1, 1, 0, 0)),
    ([page_bytes(["a", "b"]), page_bytes(["c"], number_matched=6)],
     "number_matched_changed", RunCounts(2, 2, 1, 1)),
    ([page_bytes(["a", "b"]), page_bytes(["c"], number_matched=NUMBER_MATCHED_ABSENT)],
     "number_matched_changed", RunCounts(2, 2, 1, 1)),
    ([page_bytes(["a", "b", "c"])], "page_count_invalid", RunCounts(1, 1, 0, 0)),
])
def test_persisted_validation_failures_retain_artifacts_not_completion(tmp_path, steps, category, counts):
    client = FaultClient(steps + [page_bytes([])])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client)
    error = caught.value
    assert error.failure_category == category and error.counts == counts
    assert error.run_status == "failed" and len(client.calls) == len(steps)
    assert len(list(tmp_path.rglob("*.sanitized.json"))) == counts.persisted_page_count
    assert len(list(journal_directory(tmp_path).glob("page_*.json"))) == counts.journaled_page_count
    assert not (journal_directory(tmp_path) / "run_complete.json").exists()
    reference = error.unjournaled_page
    assert reference.outcome_stage == "persisted_unvalidated"
    assert reference.requested_offset == (len(steps) - 1) * 2
    assert reference.requested_limit == 2
    terminal = json.loads(error.terminal_path.read_bytes())
    assert terminal["unjournaled_page"] == reference.to_dict()
    artifact = tmp_path / reference.relative_artifact_path
    metadata = tmp_path / reference.relative_metadata_path
    assert artifact.exists() and metadata.exists()
    assert json.loads(metadata.read_bytes())["stored_artifact_sha256"] == reference.stored_artifact_sha256
    assert DUMMY_KEY not in repr(reference)
    assert_safe_outcome(error)


def test_intermediate_failure_preserves_prior_artifacts(tmp_path):
    client = FaultClient([page_bytes(["a", "b"]), b"malformed", page_bytes([])])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client)
    assert caught.value.counts == RunCounts(2, 1, 1, 1)
    assert len(client.calls) == 2 and len(list(tmp_path.rglob("*.sanitized.json"))) == 1
    terminal = json.loads(caught.value.terminal_path.read_bytes())
    assert terminal["last_successful_offset"] == 0 and terminal["successful_page_count"] == 1


def test_maximum_page_exhaustion_is_failed_not_complete(tmp_path):
    client = FaultClient([page_bytes(["a", "b"]), page_bytes([])])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client, max_pages=1)
    assert caught.value.failure_category == "max_pages_exhausted"
    assert caught.value.counts == RunCounts(1, 1, 1, 1)
    assert caught.value.run_status == "failed" and len(client.calls) == 1


def test_idempotent_source_reuse_counts_successful_returns(tmp_path):
    first = journaled(tmp_path, FaultClient([page_bytes([])]), run_id="first")
    second = journaled(tmp_path, FaultClient([page_bytes([])]), run_id="second")
    assert first.pages[0].created and not second.pages[0].created
    assert first.pages[0].artifact_path == second.pages[0].artifact_path
    assert second.counts == RunCounts(1, 1, 1, 1)


def test_journal_creation_collision_is_safe_and_pre_dispatch(tmp_path):
    result = journaled(tmp_path, FaultClient([page_bytes([])]))
    before = {p.name: p.read_bytes() for p in result.run_directory.iterdir()}
    client = FaultClient([])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client)
    assert caught.value.failure_category == "journal_start_failed"
    assert caught.value.run_status == "not_started" and client.calls == []
    assert caught.value.counts == RunCounts(0, 0, 0, 0)
    assert before == {p.name: p.read_bytes() for p in result.run_directory.iterdir()}


@pytest.mark.parametrize("target,category,expected", [
    ("page_000000.json", "journal_page_failed", RunCounts(1, 1, 1, 0)),
    ("run_complete.json", "completion_terminal_failed", RunCounts(1, 1, 1, 1)),
    ("source", "source_publication_failed", RunCounts(1, 0, 0, 0)),
])
def test_pre_link_failures_attempt_one_failure_terminal(tmp_path, monkeypatch, target, category, expected):
    real_link = run_manifest.os.link
    links = []
    def link(source, destination):
        name = Path(destination).name
        links.append(name)
        if name == target or (target == "source" and name.endswith(".sanitized.json")):
            raise OSError(DUMMY_KEY)
        return real_link(source, destination)
    monkeypatch.setattr(run_manifest.os, "link", link)
    client = FaultClient([page_bytes([]), page_bytes([])])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client)
    error = caught.value
    assert error.failure_category == category and error.counts == expected
    assert error.run_status == "failed" and len(client.calls) == 1
    if target == "page_000000.json":
        assert error.unjournaled_page.outcome_stage == "validated_unjournaled"
        assert json.loads(error.terminal_path.read_bytes())["unjournaled_page"] == error.unjournaled_page.to_dict()
    else:
        assert error.unjournaled_page is None
        assert "unjournaled_page" not in json.loads(error.terminal_path.read_bytes())
    assert links.count("run_failed.json") == 1
    assert not list(tmp_path.rglob("*.tmp"))
    assert_safe_outcome(error)


@pytest.mark.parametrize("target,status,category,expected", [
    ("page_000000.json", "interrupted", "journal_page_cleanup_failed", RunCounts(1, 1, 1, 1)),
    ("run_complete.json", "complete", "completion_cleanup_failed", RunCounts(1, 1, 1, 1)),
    ("run_failed.json", "failed", "request_timeout", RunCounts(1, 0, 0, 0)),
])
def test_published_cleanup_fault_respects_disk_and_journal_state(
    tmp_path, monkeypatch, target, status, category, expected,
):
    captured = []
    original_publish = RunJournal._publish
    def publish(self, *args):
        captured.append(self)
        return original_publish(self, *args)
    monkeypatch.setattr(RunJournal, "_publish", publish)
    real_remove = run_manifest._remove_temp
    def remove(path):
        return False if Path(path).name.startswith(f".{target}.") else real_remove(path)
    monkeypatch.setattr(run_manifest, "_remove_temp", remove)
    step = GistdaTimeoutError() if status == "failed" else page_bytes([])
    client = FaultClient([step, page_bytes([])])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client)
    error = caught.value
    journal = captured[-1]
    assert error.failure_category == category and error.run_status == status
    assert error.record_published and error.cleanup_failed
    assert error.counts == journal.counts == expected
    assert error.unjournaled_page is None
    assert journal.page_count == expected.journaled_page_count
    assert journal.is_terminal == (status != "interrupted")
    assert journal.cleanup_failed and len(client.calls) == 1
    assert (journal.run_directory / target).exists()
    assert (journal.run_directory / "run_failed.json").exists() == (status == "failed")
    assert (journal.run_directory / "run_complete.json").exists() == (status == "complete")
    if status == "failed":
        assert error.terminal_failure_category == "failure_terminal_cleanup_failed"
    assert_safe_outcome(error)
    monkeypatch.setattr(run_manifest, "_remove_temp", real_remove)
    for temporary in journal.run_directory.glob("*.tmp"):
        assert real_remove(str(temporary))


@pytest.mark.parametrize("cleanup_fault", [False, True])
def test_failure_terminal_pre_link_failure_is_interrupted_without_retry(tmp_path, monkeypatch, cleanup_fault):
    original_link = run_manifest.os.link
    original_remove = run_manifest._remove_temp
    calls = []
    def link(source, destination):
        calls.append(Path(destination).name)
        if Path(destination).name == "run_failed.json":
            raise OSError(DUMMY_KEY)
        return original_link(source, destination)
    monkeypatch.setattr(run_manifest.os, "link", link)
    if cleanup_fault:
        def remove(path):
            return False if Path(path).name.startswith(".run_failed.json.") else original_remove(path)
        monkeypatch.setattr(run_manifest, "_remove_temp", remove)
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, FaultClient([GistdaTimeoutError()]))
    error = caught.value
    assert error.failure_category == "request_timeout"
    assert error.terminal_failure_category == "failure_terminal_failed"
    assert error.run_status == "interrupted" and calls.count("run_failed.json") == 1
    assert error.counts == RunCounts(1, 0, 0, 0)
    assert error.cleanup_failed == cleanup_fault and not error.record_published
    assert not (journal_directory(tmp_path) / "run_failed.json").exists()
    assert bool(list(tmp_path.rglob("*.tmp"))) == cleanup_fault
    assert_safe_outcome(error)
    monkeypatch.setattr(run_manifest, "_remove_temp", original_remove)
    for temporary in journal_directory(tmp_path).glob("*.tmp"):
        assert original_remove(str(temporary))


def test_process_interruption_propagates_without_terminal(tmp_path):
    client = FaultClient([page_bytes(["a", "b"]), KeyboardInterrupt(), page_bytes([])])
    with pytest.raises(KeyboardInterrupt):
        journaled(tmp_path, client)
    directory = journal_directory(tmp_path)
    assert (directory / "page_000000.json").exists()
    assert not (directory / "run_failed.json").exists()
    assert not (directory / "run_complete.json").exists()
    assert len(client.calls) == 2


@pytest.mark.parametrize("target", ["run_started.json", "page_000000.json", "run_complete.json"])
def test_journal_temp_setup_failure_has_stage_aware_outcome(tmp_path, monkeypatch, target):
    original_write = run_manifest._write_temp
    def write(directory, name, content):
        if name == target:
            raise RunJournalError("temporary_write_failed")
        return original_write(directory, name, content)
    monkeypatch.setattr(run_manifest, "_write_temp", write)
    client = FaultClient([page_bytes([]), page_bytes([])])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client)
    error = caught.value
    if target == "run_started.json":
        assert error.run_status == "not_started" and client.calls == []
        assert error.counts == RunCounts(0, 0, 0, 0)
        assert not journal_directory(tmp_path).exists()
    else:
        assert error.run_status == "failed" and len(client.calls) == 1
        assert error.counts == RunCounts(1, 1, 1, int(target == "run_complete.json"))
        assert len(list(tmp_path.rglob("*.sanitized.json"))) == 1
    assert not list(tmp_path.rglob("*.tmp"))
    assert_safe_outcome(error)


def test_start_publication_cleanup_fault_is_interrupted_pre_dispatch(tmp_path, monkeypatch):
    real_remove = run_manifest._remove_temp
    monkeypatch.setattr(run_manifest, "_remove_temp", lambda path: False)
    client = FaultClient([])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client)
    error = caught.value
    assert error.run_status == "interrupted" and error.run_directory == journal_directory(tmp_path)
    assert error.record_published and error.cleanup_failed and client.calls == []
    assert error.counts == RunCounts(0, 0, 0, 0)
    assert (error.run_directory / "run_started.json").exists()
    assert not (error.run_directory / "run_failed.json").exists()
    assert_safe_outcome(error)
    monkeypatch.setattr(run_manifest, "_remove_temp", real_remove)
    for path in error.run_directory.glob("*.tmp"):
        assert real_remove(str(path))


@pytest.mark.parametrize("target", ["page_000000.json", "run_complete.json"])
def test_pre_link_cleanup_failure_blocks_failure_terminal(tmp_path, monkeypatch, target):
    original_link = run_manifest.os.link
    original_remove = run_manifest._remove_temp
    links = []
    def link(source, destination):
        links.append(Path(destination).name)
        if Path(destination).name == target:
            raise OSError(DUMMY_KEY)
        return original_link(source, destination)
    def remove(path):
        return False if Path(path).name.startswith(f".{target}.") else original_remove(path)
    monkeypatch.setattr(run_manifest.os, "link", link)
    monkeypatch.setattr(run_manifest, "_remove_temp", remove)
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, FaultClient([page_bytes([])]))
    error = caught.value
    assert error.run_status == "interrupted" and error.cleanup_failed
    assert not error.record_published
    assert error.counts == RunCounts(1, 1, 1, int(target == "run_complete.json"))
    assert "run_failed.json" not in links
    assert len(list(tmp_path.rglob("*.sanitized.json"))) == 1
    assert_safe_outcome(error)
    monkeypatch.setattr(run_manifest, "_remove_temp", original_remove)
    for path in journal_directory(tmp_path).glob("*.tmp"):
        assert original_remove(str(path))


def test_intermediate_source_publication_failure_preserves_prior_page(tmp_path, monkeypatch):
    original_link = run_manifest.os.link
    def link(source, destination):
        name = Path(destination).name
        if "__offset-2__" in name:
            raise OSError(DUMMY_KEY)
        return original_link(source, destination)
    monkeypatch.setattr(run_manifest.os, "link", link)
    client = FaultClient([page_bytes(["a", "b"]), page_bytes(["c"]), page_bytes([])])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client)
    assert caught.value.failure_category == "source_publication_failed"
    assert caught.value.counts == RunCounts(2, 1, 1, 1)
    assert caught.value.unjournaled_page is None
    assert len(client.calls) == 2 and len(list(tmp_path.rglob("*.sanitized.json"))) == 1
    assert not list(tmp_path.rglob("*.tmp"))


def test_missing_ids_report_incomplete_journaled_summary(tmp_path):
    result = journaled(tmp_path, FaultClient([page_bytes([None])]))
    terminal = json.loads(result.terminal_path.read_bytes())
    assert result.missing_feature_id_count == 1 and not result.duplicate_id_check_complete
    assert terminal["duplicate_id_check_complete"] is False


@pytest.mark.parametrize("stage", ["start", "complete", "failure"])
def test_lifecycle_clock_failure_is_safe_and_not_retried(tmp_path, stage):
    evaluations = []
    def clock():
        evaluations.append(None)
        if stage == "start" or len(evaluations) == 2:
            raise RuntimeError(DUMMY_KEY)
        return FIXED_TIME
    step = GistdaTimeoutError() if stage == "failure" else page_bytes([])
    client = FaultClient([step])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client, clock=clock)
    error = caught.value
    assert len(client.calls) == (0 if stage == "start" else 1)
    expected_status = {"start": "not_started", "complete": "failed", "failure": "interrupted"}[stage]
    assert error.run_status == expected_status
    assert not list(tmp_path.rglob("run_complete.json"))
    assert bool(list(tmp_path.rglob("run_failed.json"))) == (stage == "complete")
    assert len(evaluations) == {"start": 1, "complete": 3, "failure": 2}[stage]
    assert_safe_outcome(error)


def test_journal_append_validation_rejection_preserves_artifact_and_counts(tmp_path, monkeypatch):
    captured = []
    def reject(self, record, *, counts):
        captured.append((self, self.counts))
        raise ValueError(DUMMY_KEY)
    monkeypatch.setattr(RunJournal, "append_page", reject)
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, FaultClient([page_bytes([])]))
    journal, previous = captured[0]
    assert previous == RunCounts(0, 0, 0, 0)
    assert caught.value.counts == journal.counts == RunCounts(1, 1, 1, 0)
    assert journal.page_count == 0 and journal.status == "failed"
    assert len(list(tmp_path.rglob("*.sanitized.json"))) == 1
    assert not list(journal.run_directory.glob("page_*.json"))
    assert_safe_outcome(caught.value)


@pytest.mark.parametrize("overrides", [
    {"limit": True}, {"limit": 0}, {"max_pages": True}, {"max_pages": 0},
    {"config": GistdaConfig(api_base_url=OFFICIAL_GISTDA_API_BASE_URL, api_key=DUMMY_KEY, province_id="95")},
    {"config": GistdaConfig(api_base_url="https://unapproved.example.test", api_key=DUMMY_KEY, province_id="94")},
    {"retrieved_at": FIXED_TIME.replace(tzinfo=None)}, {"clock": "not-callable"},
])
def test_invalid_journal_inputs_fail_before_clock_dispatch_or_disk(tmp_path, overrides):
    client = FaultClient([])
    def forbidden():
        raise AssertionError("invalid inputs evaluated lifecycle clock")
    arguments = dict(config=config(), client=client, output_root=tmp_path,
                     limit=2, max_pages=10, run_id="integrated",
                     retrieved_at=FIXED_TIME, clock=forbidden)
    arguments.update(overrides)
    with pytest.raises(ValueError) as caught:
        paginate_pattani(**arguments)
    assert DUMMY_KEY not in str(caught.value) + repr(caught.value)
    assert client.calls == [] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("invalid_id", ["../outside", DUMMY_KEY])
def test_invalid_or_secret_run_id_creates_no_journal_or_request(tmp_path, invalid_id):
    client = FaultClient([])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client, run_id=invalid_id)
    assert caught.value.failure_category == "journal_start_failed"
    assert caught.value.run_status == "not_started" and client.calls == []
    assert list(tmp_path.iterdir()) == []
    assert_safe_outcome(caught.value)


def test_untrusted_policy_error_category_is_not_retained(tmp_path):
    unsafe = PaginationPolicyError(DUMMY_KEY, failure_category=DUMMY_KEY)
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, FaultClient([unsafe]))
    assert caught.value.failure_category == "unexpected_failure"
    assert_safe_outcome(caught.value)


@pytest.mark.parametrize("boundary", ["validation", "append"])
@pytest.mark.parametrize("terminal_fault", ["before_publication", "after_publication"])
def test_unjournaled_lineage_survives_failure_terminal_fault(
    tmp_path, monkeypatch, boundary, terminal_fault,
):
    original_link = run_manifest.os.link
    original_remove = run_manifest._remove_temp
    def link(source, destination):
        name = Path(destination).name
        if (boundary == "append" and name == "page_000000.json") or (
            terminal_fault == "before_publication" and name == "run_failed.json"
        ):
            raise OSError(DUMMY_KEY)
        return original_link(source, destination)
    def remove(path):
        if terminal_fault == "after_publication" and Path(path).name.startswith(".run_failed.json."):
            return False
        return original_remove(path)
    monkeypatch.setattr(run_manifest.os, "link", link)
    monkeypatch.setattr(run_manifest, "_remove_temp", remove)
    response = page_bytes(["duplicate", "duplicate"]) if boundary == "validation" else page_bytes([])
    client = FaultClient([response, page_bytes([])])
    with pytest.raises(PaginationRunError) as caught:
        journaled(tmp_path, client)
    error = caught.value
    reference = error.unjournaled_page
    expected_stage = "persisted_unvalidated" if boundary == "validation" else "validated_unjournaled"
    assert reference.outcome_stage == expected_stage
    assert error.counts == RunCounts(1, 1, int(boundary == "append"), 0)
    assert len(client.calls) == 1
    assert reference.requested_offset == 0 and reference.requested_limit == 2
    artifact = tmp_path / reference.relative_artifact_path
    metadata = tmp_path / reference.relative_metadata_path
    assert artifact.exists() and metadata.exists()
    assert not list(journal_directory(tmp_path).glob("page_*.json"))
    if terminal_fault == "after_publication":
        assert error.run_status == "failed" and error.cleanup_failed
        assert json.loads(error.terminal_path.read_bytes())["unjournaled_page"] == reference.to_dict()
    else:
        assert error.run_status == "interrupted"
        assert not (journal_directory(tmp_path) / "run_failed.json").exists()
    assert_safe_outcome(error)
    assert repr(reference) == "UnjournaledPageReference()"
    assert DUMMY_KEY not in json.dumps(reference.to_dict())
    with pytest.raises(AttributeError):
        reference.requested_offset = 99
    monkeypatch.setattr(run_manifest, "_remove_temp", original_remove)
    for temporary in journal_directory(tmp_path).glob("*.tmp"):
        assert original_remove(str(temporary))


class SequencedClient:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get_flood_frequency(self, **kwargs: Any) -> GistdaResponse:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("pagination requested an unexpected page")
        return GistdaResponse(
            self.responses.pop(0),
            200,
            "application/geo+json",
        )


@pytest.fixture(autouse=True)
def prohibit_external_access(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_network(*_: object, **__: object) -> None:
        raise AssertionError("live network access is prohibited")

    def fail_dotenv(*_: object, **__: object) -> None:
        raise AssertionError("dotenv access is prohibited")

    monkeypatch.setattr(requests.sessions.Session, "request", fail_network)
    monkeypatch.setattr("src.configuration.dotenv_values", fail_dotenv)


def config() -> GistdaConfig:
    return GistdaConfig(
        api_base_url=OFFICIAL_GISTDA_API_BASE_URL,
        api_key=DUMMY_KEY,
        province_id="94",
    )


def paginate(tmp_path: Path, client: SequencedClient, *, limit: int = 2, max_pages: int = 10):
    return paginate_pattani(
        config=config(),
        client=client,  # type: ignore[arg-type]
        output_root=tmp_path,
        limit=limit,
        max_pages=max_pages,
        retrieved_at=FIXED_TIME,
    )


def assert_artifacts_are_credential_safe(result: Any) -> None:
    for page in result.pages:
        artifact_bytes = page.artifact_path.read_bytes()
        metadata_bytes = page.metadata_path.read_bytes()
        assert DUMMY_KEY.encode() not in artifact_bytes
        assert DUMMY_KEY.encode() not in metadata_bytes
        payload = json.loads(artifact_bytes)
        for link in payload["links"]:
            names = {
                name.casefold()
                for name, _ in parse_qsl(urlsplit(link["href"]).query)
            }
            assert "api_key" not in names


def test_full_pages_then_partial_page_use_exact_offsets_and_ignore_links(
    tmp_path: Path,
) -> None:
    client = SequencedClient(
        [
            page_bytes(["a", "b"], next_offset=700),
            page_bytes(["c", "d"], next_offset=800),
            page_bytes(["e"], next_offset=900),
        ]
    )

    result = paginate(tmp_path, client)

    assert result.requested_offsets == (0, 2, 4)
    assert [call["offset"] for call in client.calls] == [0, 2, 4]
    assert all(call["limit"] == 2 and call["pv_idn"] == "94" for call in client.calls)
    assert result.stop_reason == "partial_page"
    assert result.total_features == 5
    assert result.observed_number_matched == 5
    assert result.duplicate_id_check_complete is True
    assert result.to_dict()["page_count"] == 3
    assert_artifacts_are_credential_safe(result)


def test_empty_first_page_stops_immediately(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes([], number_matched=0)])

    result = paginate(tmp_path, client)

    assert result.requested_offsets == (0,)
    assert result.stop_reason == "empty_page"
    assert result.total_features == 0


def test_empty_final_page_after_full_page(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes(["a", "b"], number_matched=2), page_bytes([], number_matched=2)])

    result = paginate(tmp_path, client)

    assert result.requested_offsets == (0, 2)
    assert result.stop_reason == "empty_page"


def test_repeated_page_content_fails(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes(["a", "b"]), page_bytes(["a", "b"])])

    with pytest.raises(PaginationPolicyError, match="repeated"):
        paginate(tmp_path, client)


def test_duplicate_ids_across_pages_fail(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes(["a", "b"]), page_bytes(["b", "c"])])

    with pytest.raises(PaginationPolicyError, match="duplicate"):
        paginate(tmp_path, client)


def test_missing_ids_report_incomplete_duplicate_verification(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes(["a", None])])

    result = paginate(tmp_path, client, limit=3)

    assert result.stop_reason == "partial_page"
    assert result.duplicate_id_check_complete is False
    assert result.missing_feature_id_count == 1


@pytest.mark.parametrize(
    ("label", "feature"),
    [
        ("missing", {"type": "Feature"}),
        ("null", {"id": None, "type": "Feature"}),
        ("empty", {"id": "", "type": "Feature"}),
        ("boolean", {"id": True, "type": "Feature"}),
        ("numeric", {"id": 7, "type": "Feature"}),
        ("list", {"id": ["a"], "type": "Feature"}),
        ("object", {"id": {"value": "a"}, "type": "Feature"}),
    ],
    ids=lambda item: item if isinstance(item, str) else None,
)
def test_unusable_feature_ids_increment_incomplete_count(
    tmp_path: Path, label: str, feature: dict[str, object]
) -> None:
    payload = {
        "features": [feature],
        "numberReturned": 1,
        "numberMatched": 1,
    }
    client = SequencedClient([json.dumps(payload).encode("utf-8")])

    result = paginate(tmp_path, client, limit=2)

    assert result.missing_feature_id_count == 1, label
    assert result.duplicate_id_check_complete is False
    assert result.pages[0].feature_ids == ()


@pytest.mark.parametrize("feature", [None, True, 7, "feature", [], ["item"]])
def test_non_object_features_fail(
    tmp_path: Path, feature: object
) -> None:
    payload = {
        "features": [feature],
        "numberReturned": 1,
        "numberMatched": 1,
    }
    client = SequencedClient([json.dumps(payload).encode("utf-8")])

    with pytest.raises(SourcePageStructureError, match="JSON object"):
        paginate(tmp_path, client, limit=2)

    assert not client.responses
    assert list(tmp_path.iterdir()) == []


def test_changing_number_matched_fails(tmp_path: Path) -> None:
    client = SequencedClient(
        [page_bytes(["a", "b"], number_matched=4), page_bytes(["c"], number_matched=5)]
    )

    with pytest.raises(PaginationPolicyError, match="numberMatched"):
        paginate(tmp_path, client)


def test_number_matched_missing_then_present_fails(tmp_path: Path) -> None:
    client = SequencedClient(
        [
            page_bytes(["a", "b"], number_matched=NUMBER_MATCHED_ABSENT),
            page_bytes(["c"], number_matched=3),
        ]
    )

    with pytest.raises(PaginationPolicyError, match="presence"):
        paginate(tmp_path, client)


def test_number_matched_present_then_missing_fails(tmp_path: Path) -> None:
    client = SequencedClient(
        [
            page_bytes(["a", "b"], number_matched=3),
            page_bytes(["c"], number_matched=NUMBER_MATCHED_ABSENT),
        ]
    )

    with pytest.raises(PaginationPolicyError, match="presence"):
        paginate(tmp_path, client)


def test_number_matched_consistently_absent_is_allowed(tmp_path: Path) -> None:
    client = SequencedClient(
        [
            page_bytes(["a", "b"], number_matched=NUMBER_MATCHED_ABSENT),
            page_bytes(["c"], number_matched=NUMBER_MATCHED_ABSENT),
        ]
    )

    result = paginate(tmp_path, client)

    assert result.observed_number_matched is None
    assert result.stop_reason == "partial_page"


def test_number_matched_consistently_stable_is_allowed(tmp_path: Path) -> None:
    client = SequencedClient(
        [page_bytes(["a", "b"], number_matched=3), page_bytes(["c"], number_matched=3)]
    )

    result = paginate(tmp_path, client)

    assert result.observed_number_matched == 3
    assert result.stop_reason == "partial_page"


def test_malformed_intermediate_page_stops_without_extra_request(tmp_path: Path) -> None:
    client = SequencedClient([page_bytes(["a", "b"]), b"not-json"])
    run_result = None

    with pytest.raises(SourceSanitizationError):
        run_result = paginate(tmp_path, client)

    assert run_result is None
    assert [call["offset"] for call in client.calls] == [0, 2]
    artifacts = list(tmp_path.rglob("*.sanitized.json"))
    metadata = list(tmp_path.rglob("*.metadata.json"))
    assert len(artifacts) == 1
    assert len(metadata) == 1
    assert not list(tmp_path.rglob("*manifest*"))


@pytest.mark.parametrize(
    "payload",
    [
        {"numberReturned": 0},
        {"features": {}, "numberReturned": 0},
        {"features": [], "numberReturned": -1},
        {"features": [], "numberReturned": True},
        {"features": [], "numberReturned": 1},
        {"features": [], "numberReturned": 0, "numberMatched": -1},
    ],
)
def test_invalid_page_structure_or_counts_fail(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    client = SequencedClient([json.dumps(payload).encode("utf-8")])

    with pytest.raises(SourcePageStructureError):
        paginate(tmp_path, client)

    assert list(tmp_path.iterdir()) == []


def test_maximum_page_cap_fails_after_configured_number_of_pages(
    tmp_path: Path,
) -> None:
    client = SequencedClient([page_bytes(["a", "b"])])

    with pytest.raises(PaginationPolicyError, match="maximum"):
        paginate(tmp_path, client, max_pages=1)

    assert len(client.calls) == 1
