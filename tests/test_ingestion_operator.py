from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from src import configuration
from src.configuration import GistdaConfig, OFFICIAL_GISTDA_API_BASE_URL
from src.ingestion import operator
from src.ingestion.operator import (
    GitInspection, OperatorServices, main, preflight, run_ingestion,
    verify_existing_run,
)
from src.ingestion.pagination import PaginationRunError, PaginationRunResult
from src.ingestion.run_manifest import RunCounts
from src.ingestion.run_verification import RunVerificationReport

KEY = "dummy-operator-secret"
NOW = datetime(2026, 9, 19, 1, 2, 3, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network or real configuration access")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(requests, "request", forbidden)
    monkeypatch.setattr(configuration, "dotenv_values", forbidden)


@pytest.fixture
def repository(tmp_path):
    (tmp_path / "data/raw").mkdir(parents=True)
    return tmp_path


class Client:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class Recorder:
    def __init__(self):
        self.config_calls = 0
        self.client_calls = 0
        self.paginate_calls = []
        self.verify_calls = []
        self.client = Client()
        self.git = GitInspection(True, True, True, True)
        self.free = 1000
        self.result = None
        self.failure = None
        self.verification = RunVerificationReport(
            "run-1", "complete", terminal_kind="complete",
            counts_final=True, configured_key_check_complete=True,
        )

    def config(self):
        self.config_calls += 1
        return GistdaConfig(OFFICIAL_GISTDA_API_BASE_URL, KEY, "94")

    def client_factory(self, config):
        assert config.api_key == KEY
        self.client_calls += 1
        return self.client

    def paginator(self, **kwargs):
        self.paginate_calls.append(kwargs)
        if self.failure:
            raise self.failure
        if self.result:
            return self.result
        root = Path(kwargs["output_root"])
        return PaginationRunResult(
            pages=(), requested_offsets=(0,), stop_reason="empty_page", total_features=0,
            observed_number_matched=None, duplicate_id_check_complete=True,
            missing_feature_id_count=0, run_id=kwargs["run_id"],
            run_directory=root / "run", terminal_path=root / "run/run_complete.json",
            counts=RunCounts(1, 1, 1, 1),
        )

    def verifier(self, output_root, run_id, *, api_key=None):
        self.verify_calls.append((Path(output_root), run_id, api_key))
        return self.verification

    def git_inspector(self, repository, run_id):
        return self.git

    def services(self):
        return OperatorServices(
            self.config, self.client_factory, self.paginator, self.verifier,
            self.git_inspector, lambda path: SimpleNamespace(free=self.free),
            lambda: NOW, lambda: "fixedtoken",
        )


def ready(repository, recorder, **changes):
    values = dict(run_id="run-1", limit=10, max_pages=10, min_free_bytes=100,
                  load_local_config=True, repository_root=repository,
                  services=recorder.services())
    values.update(changes)
    return preflight(**values)


def events():
    values = []
    return values, values.append


def test_preflight_success_and_probe_cleanup(repository):
    recorder = Recorder()
    report, config = ready(repository, recorder)
    assert report.ready and not report.issue_categories
    assert report.to_dict()["output_root"] == "data/raw"
    assert report.measured_free_bytes == 1000 and config.api_key == KEY
    assert list((repository / "data/raw").iterdir()) == []


def test_preflight_without_config_is_incomplete_and_no_load(repository):
    recorder = Recorder()
    report, config = ready(repository, recorder, load_local_config=False)
    assert not report.ready and "configuration_incomplete" in report.issue_categories
    assert config is None and recorder.config_calls == 0 and recorder.client_calls == 0


@pytest.mark.parametrize("field,value,category", [
    ("run_id", "../escape", "invalid_run_id"),
    ("run_id", "NUL", "invalid_run_id"),
    ("limit", 0, "invalid_limit"), ("limit", 10001, "invalid_limit"),
    ("limit", True, "invalid_limit"), ("max_pages", 0, "invalid_max_pages"),
    ("max_pages", 100001, "invalid_max_pages"), ("max_pages", True, "invalid_max_pages"),
    ("min_free_bytes", 0, "invalid_min_free_bytes"),
    ("min_free_bytes", True, "invalid_min_free_bytes"),
])
def test_parameter_refusals(repository, field, value, category):
    recorder = Recorder()
    report, _ = ready(repository, recorder, **{field: value})
    assert not report.ready and category in report.issue_categories


@pytest.mark.parametrize("limit", [1, 10000])
@pytest.mark.parametrize("pages", [1, 100000])
def test_limit_and_page_boundaries(repository, limit, pages):
    report, _ = ready(repository, Recorder(), limit=limit, max_pages=pages)
    assert report.ready


@pytest.mark.parametrize("free,floor,ready_expected", [(99, 100, False), (100, 100, True), (101, 100, True)])
def test_disk_floor(repository, free, floor, ready_expected):
    recorder = Recorder()
    recorder.free = free
    report, _ = ready(repository, recorder, min_free_bytes=floor)
    assert report.ready is ready_expected
    assert ("insufficient_free_space" in report.issue_categories) is (not ready_expected)


def test_disk_measurement_failure(repository):
    recorder = Recorder()
    services = recorder.services()
    services = OperatorServices(
        services.config_loader, services.client_factory, services.paginator, services.verifier,
        services.git_inspector, lambda path: (_ for _ in ()).throw(OSError(KEY)),
        services.clock, services.probe_token,
    )
    report, _ = preflight(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                          load_local_config=True, repository_root=repository, services=services)
    assert "disk_measurement_failed" in report.issue_categories
    assert KEY not in repr(report)


@pytest.mark.parametrize("attribute,category", [
    ("fsync", "probe_fsync_failed"), ("link", "probe_hardlink_failed"),
])
def test_probe_system_failures(repository, monkeypatch, attribute, category):
    recorder = Recorder()
    monkeypatch.setattr(operator.os, attribute,
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError(KEY)))
    report, _ = ready(repository, recorder)
    assert category in report.issue_categories and not report.ready
    assert list((repository / "data/raw").iterdir()) == []


def test_probe_flush_failure(repository, monkeypatch):
    recorder = Recorder()
    original = Path.open

    class BrokenFlush:
        def __init__(self, wrapped): self.wrapped = wrapped
        def __enter__(self): return self
        def __exit__(self, *args): self.wrapped.close()
        def write(self, value): return self.wrapped.write(value)
        def flush(self): raise OSError(KEY)
        def fileno(self): return self.wrapped.fileno()

    def open_file(path, *args, **kwargs):
        wrapped = original(path, *args, **kwargs)
        return BrokenFlush(wrapped) if path.name == "source.probe" else wrapped

    monkeypatch.setattr(Path, "open", open_file)
    report, _ = ready(repository, recorder)
    assert "probe_flush_failed" in report.issue_categories
    assert list((repository / "data/raw").iterdir()) == []


def test_probe_write_failure(repository, monkeypatch):
    recorder = Recorder()
    original = Path.open
    def open_file(path, *args, **kwargs):
        if path.name == "source.probe": raise OSError(KEY)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", open_file)
    report, _ = ready(repository, recorder)
    assert "probe_write_failed" in report.issue_categories


def test_probe_cleanup_failure_refuses(repository, monkeypatch):
    recorder = Recorder()
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.name == "linked.probe": raise OSError(KEY)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    report, _ = ready(repository, recorder)
    assert not report.ready and "probe_cleanup_failed" in report.issue_categories


def test_probe_stage_and_cleanup_failures_are_both_reported(repository, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(operator.os, "fsync",
                        lambda *args: (_ for _ in ()).throw(OSError(KEY)))
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path.name == "source.probe": raise OSError(KEY)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    report, _ = ready(repository, recorder)
    assert {"probe_fsync_failed", "probe_cleanup_failed"}.issubset(report.issue_categories)


def test_probe_directory_creation_failure(repository, monkeypatch):
    recorder = Recorder()
    original = Path.mkdir
    def mkdir(path, *args, **kwargs):
        if path.name.startswith(".operator-preflight-"): raise OSError(KEY)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    report, _ = ready(repository, recorder)
    assert "probe_directory_failed" in report.issue_categories


def test_probe_collision_preserves_existing_directory(repository):
    recorder = Recorder()
    existing = repository / "data/raw/.operator-preflight-fixedtoken"
    existing.mkdir()
    marker = existing / "existing"
    marker.write_text("preserve", encoding="utf-8")
    report, _ = ready(repository, recorder)
    assert "probe_collision" in report.issue_categories
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_invalid_probe_token_refuses_without_probe(repository):
    recorder = Recorder(); services = recorder.services()
    services = OperatorServices(
        services.config_loader, services.client_factory, services.paginator, services.verifier,
        services.git_inspector, services.disk_usage, services.clock, lambda: "../escape",
    )
    report, _ = preflight(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                          load_local_config=True, repository_root=repository, services=services)
    assert "probe_setup_failed" in report.issue_categories
    assert list((repository / "data/raw").iterdir()) == []


@pytest.mark.parametrize("inspection,category", [
    (GitInspection(False, True, True, True), "git_state_dirty"),
    (GitInspection(True, False, True, True), "env_not_ignored"),
    (GitInspection(True, True, True, True, False), "env_tracked"),
    (GitInspection(True, True, False, True), "prospective_output_not_ignored"),
    (GitInspection(True, True, True, False), "prospective_output_tracked"),
])
def test_git_refusals(repository, inspection, category):
    recorder = Recorder(); recorder.git = inspection
    report, _ = ready(repository, recorder)
    assert category in report.issue_categories


def test_dirty_git_explicit_acknowledgement(repository):
    recorder = Recorder(); recorder.git = GitInspection(False, True, True, True)
    report, _ = ready(repository, recorder, acknowledge_dirty=True)
    assert report.ready and report.git_dirty_acknowledged


def test_git_inspection_failure(repository):
    recorder = Recorder()
    services = recorder.services()
    services = OperatorServices(
        services.config_loader, services.client_factory, services.paginator, services.verifier,
        lambda *args: (_ for _ in ()).throw(OSError(KEY)), services.disk_usage,
        services.clock, services.probe_token,
    )
    report, _ = preflight(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                          load_local_config=True, repository_root=repository, services=services)
    assert "git_inspection_failed" in report.issue_categories


def test_exact_prospective_git_path_matrix_and_missing_class_refusal(repository, monkeypatch):
    recorder = Recorder()
    calls = []
    missing = "data/raw/gistda/flood_freq/pattani/prospective.metadata.json"

    def fake_git(arguments, root):
        calls.append(tuple(arguments))
        command = arguments[0]
        path = arguments[-1] if "--" in arguments else None
        if command == "status":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == "check-ignore":
            return SimpleNamespace(returncode=1 if path == missing else 0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(operator, "_git", fake_git)
    services = recorder.services()
    services = OperatorServices(
        services.config_loader, services.client_factory, services.paginator, services.verifier,
        operator._inspect_git, services.disk_usage, services.clock, services.probe_token,
    )
    report, _ = preflight(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                          load_local_config=True, repository_root=repository, services=services)
    expected = {
        "data/raw/gistda/flood_freq/pattani/prospective.sanitized.json",
        "data/raw/gistda/flood_freq/pattani/prospective.metadata.json",
        "data/raw/gistda/flood_freq/pattani/runs/run-1/run_started.json",
        "data/raw/gistda/flood_freq/pattani/runs/run-1/page_000000.json",
        "data/raw/gistda/flood_freq/pattani/runs/run-1/run_complete.json",
        "data/raw/gistda/flood_freq/pattani/runs/run-1/run_failed.json",
        "data/raw/gistda/flood_freq/pattani/.prospective.sanitized.json.operator-probe.tmp",
        "data/raw/gistda/flood_freq/pattani/runs/run-1/.page_000000.json.operator-probe.tmp",
    }
    ignored = {call[-1] for call in calls if call[0] == "check-ignore" and call[-1] != ".env"}
    tracked = {call[-1] for call in calls if call[0] == "ls-files" and call[-1] != ".env"}
    assert ignored == tracked == expected
    assert not report.ready and "prospective_output_not_ignored" in report.issue_categories


def test_duplicate_run_id(repository):
    recorder = Recorder()
    (repository / "data/raw" / operator.RUNS_SUBDIRECTORY / "run-1").mkdir(parents=True)
    report, _ = ready(repository, recorder)
    assert "run_id_already_exists" in report.issue_categories


def test_missing_and_symlink_output_root(repository):
    recorder = Recorder()
    (repository / "data/raw").rmdir()
    report, _ = ready(repository, recorder)
    assert "output_root_unavailable" in report.issue_categories
    external = repository / "external"; external.mkdir()
    try:
        (repository / "data/raw").symlink_to(external, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("platform cannot create directory symlinks")
    report, _ = ready(repository, recorder)
    assert "output_root_symlink_rejected" in report.issue_categories


def test_output_root_escape_is_rejected_before_probe(repository, monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(operator, "_canonical_root",
                        lambda root: (None, "output_root_escape_rejected"))
    report, _ = ready(repository, recorder)
    assert "output_root_escape_rejected" in report.issue_categories


@pytest.mark.parametrize("config,category", [
    (GistdaConfig(OFFICIAL_GISTDA_API_BASE_URL, KEY, "95"), "province_scope_mismatch"),
    (GistdaConfig("https://unapproved.test", KEY, "94"), "base_url_mismatch"),
])
def test_configuration_scope_refusal(repository, config, category):
    recorder = Recorder()
    recorder.config = lambda: config
    report, _ = ready(repository, recorder)
    assert category in report.issue_categories


def test_configuration_failure_safe(repository):
    recorder = Recorder()
    recorder.config = lambda: (_ for _ in ()).throw(ValueError(KEY))
    report, _ = ready(repository, recorder)
    assert "configuration_invalid" in report.issue_categories and KEY not in repr(report)


def test_api_key_cannot_be_reported_as_run_id(repository):
    recorder = Recorder()
    report, _ = ready(repository, recorder, run_id=KEY)
    assert not report.ready and report.run_id is None
    assert "credential_in_run_id" in report.issue_categories and KEY not in repr(report)


@pytest.mark.parametrize("authorize,load,category", [
    (False, True, "live_authorization_flag_required"),
    (True, False, "configuration_loading_flag_required"),
    (False, False, "live_authorization_flag_required"),
])
def test_run_runtime_flags_refuse_before_preflight(repository, authorize, load, category):
    recorder = Recorder(); values, emit = events()
    code = run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                         authorize_live=authorize, load_local_config=load,
                         repository_root=repository, services=recorder.services(), emit=emit)
    assert code == 2 and category in values[-1]["issue_categories"]
    assert recorder.config_calls == recorder.client_calls == 0


def test_run_repeats_fresh_preflight_before_client(repository):
    recorder = Recorder(); values, emit = events()
    code = run_ingestion(run_id="run-1", limit=2, max_pages=3, min_free_bytes=1,
                         authorize_live=True, load_local_config=True,
                         repository_root=repository, services=recorder.services(), emit=emit)
    assert code == 0 and recorder.config_calls == recorder.client_calls == 1
    assert [event["event"] for event in values] == [
        "preflight_result", "run_start", "dispatch", "final_run_outcome"]
    call = recorder.paginate_calls[0]
    assert call["retrieved_at"] is None and call["run_id"] == "run-1"
    assert call["clock"]() == NOW and call["output_root"] == repository / "data/raw"
    assert recorder.client.closed
    assert recorder.verify_calls == [(repository / "data/raw", "run-1", KEY)]
    assert values[-1]["published_run_status"] == "complete"
    assert values[-1]["verification_status"] == "complete"
    assert values[-1]["configured_key_check_complete"] is True
    assert values[-1]["counts_final"] is True


def test_client_not_constructed_when_fresh_preflight_fails(repository):
    recorder = Recorder(); recorder.git = GitInspection(False, True, True, True)
    values, emit = events()
    code = run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                         authorize_live=True, load_local_config=True,
                         repository_root=repository, services=recorder.services(), emit=emit)
    assert code == 2 and recorder.client_calls == 0 and not recorder.paginate_calls
    assert values[-1]["status"] == "refused"


@pytest.mark.parametrize("category,status,cleanup,published", [
    ("request_timeout", "failed", False, False),
    ("source_sanitization_failed", "failed", False, False),
    ("duplicate_feature_id", "failed", False, False),
    ("journal_page_failed", "interrupted", False, False),
    ("journal_page_failed", "interrupted", True, False),
    ("journal_page_cleanup_failed", "interrupted", True, True),
    ("completion_cleanup_failed", "complete", True, True),
])
def test_safe_run_failures(repository, category, status, cleanup, published):
    recorder = Recorder()
    recorder.verification = RunVerificationReport(
        "run-1", status, terminal_kind=status if status in {"complete", "failed"} else None,
        counts_final=status in {"complete", "failed"},
        configured_key_check_complete=True,
    )
    recorder.failure = PaginationRunError(
        failure_category=category, run_status=status, counts=RunCounts(1, 1, 1, 1),
        run_directory=repository / "data/raw/run",
        terminal_path=repository / "data/raw/run/run_complete.json" if status == "complete" else None,
        cleanup_failed=cleanup, record_published=published,
    )
    values, emit = events()
    code = run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                         authorize_live=True, load_local_config=True,
                         repository_root=repository, services=recorder.services(), emit=emit)
    outcome = values[-1]
    assert code == 1 and outcome["status"] == status
    assert outcome["issue_categories"] == [category]
    assert outcome["cleanup_failed"] is cleanup and outcome["record_published"] is published
    assert KEY not in json.dumps(values) and recorder.client.closed
    assert len(recorder.verify_calls) == 1


@pytest.mark.parametrize("verified_status", ["interrupted", "complete", "failed"])
def test_keyboard_interrupt_after_client_preserves_observed_terminal(repository, verified_status):
    recorder = Recorder()
    recorder.failure = KeyboardInterrupt()
    recorder.verification = RunVerificationReport("run-1", verified_status,
                                                   terminal_kind=verified_status if verified_status != "interrupted" else None)
    values, emit = events()
    code = run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                         authorize_live=True, load_local_config=True,
                         repository_root=repository, services=recorder.services(), emit=emit)
    assert code == 130 and values[-1]["status"] == verified_status
    assert values[-1]["issue_categories"] == ["operator_interrupted"]
    assert recorder.verify_calls and recorder.client.closed


def test_keyboard_interrupt_during_preflight(repository):
    recorder = Recorder()
    recorder.config = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    values, emit = events()
    code = run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                         authorize_live=True, load_local_config=True,
                         repository_root=repository, services=recorder.services(), emit=emit)
    assert code == 130 and values == [{"event": "final_run_outcome", "run_id": "run-1",
                                      "status": "interrupted",
                                      "issue_categories": ["operator_interrupted"]}]
    assert recorder.client_calls == 0


def test_baseexception_other_than_keyboard_propagates(repository):
    recorder = Recorder(); recorder.failure = SystemExit(9)
    with pytest.raises(SystemExit) as error:
        run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                      authorize_live=True, load_local_config=True,
                      repository_root=repository, services=recorder.services(), emit=lambda event: None)
    assert error.value.code == 9


def test_ordinary_unknown_failure_is_fixed_and_safe(repository):
    recorder = Recorder(); recorder.failure = RuntimeError(KEY)
    values, emit = events()
    assert run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                         authorize_live=True, load_local_config=True,
                         repository_root=repository, services=recorder.services(), emit=emit) == 1
    assert values[-1]["issue_categories"] == ["operator_run_failed"]
    assert KEY not in json.dumps(values)


def test_complete_with_verification_warning_succeeds(repository):
    recorder = Recorder()
    recorder.verification = RunVerificationReport(
        "run-1", "complete", terminal_kind="complete", counts_final=True,
        configured_key_check_complete=True, issue_categories=("temporary_remnants",),
    )
    values, emit = events()
    code = run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                         authorize_live=True, load_local_config=True,
                         repository_root=repository, services=recorder.services(), emit=emit)
    assert code == 0 and values[-1]["published_run_status"] == "complete"
    assert values[-1]["verification_issue_categories"] == ["temporary_remnants"]
    assert values[-1]["issue_categories"] == []


@pytest.mark.parametrize("mode", ["invalid", "incomplete", "raises"])
def test_post_run_verification_failure_preserves_complete_terminal(repository, mode):
    recorder = Recorder()
    if mode == "invalid":
        recorder.verification = RunVerificationReport(
            "run-1", "invalid", issue_categories=("stored_hash_mismatch",),
            configured_key_check_complete=True,
        )
    elif mode == "incomplete":
        recorder.verification = RunVerificationReport(
            "run-1", "complete", terminal_kind="complete", counts_final=True,
            configured_key_check_complete=False,
        )
    else:
        recorder.verifier = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(KEY))
    values, emit = events()
    code = run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                         authorize_live=True, load_local_config=True,
                         repository_root=repository, services=recorder.services(), emit=emit)
    outcome = values[-1]
    assert code == 1 and outcome["status"] == "complete"
    assert outcome["published_run_status"] == "complete"
    assert outcome["issue_categories"] == ["post_run_verification_failed"]
    assert KEY not in json.dumps(values)


def test_pagination_error_verification_failure_preserves_status_and_counts(repository):
    recorder = Recorder()
    recorder.failure = PaginationRunError(
        failure_category="request_timeout", run_status="failed",
        counts=RunCounts(1, 0, 0, 0), record_published=True,
    )
    recorder.verifier = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError(KEY))
    values, emit = events()
    assert run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                         authorize_live=True, load_local_config=True,
                         repository_root=repository, services=recorder.services(), emit=emit) == 1
    outcome = values[-1]
    assert outcome["published_run_status"] == "failed"
    assert outcome["counts"] == RunCounts(1, 0, 0, 0).to_dict()
    assert outcome["issue_categories"] == ["request_timeout", "post_run_verification_failed"]


def test_keyboard_interrupt_during_post_run_verification(repository):
    recorder = Recorder()
    recorder.verifier = lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
    values, emit = events()
    code = run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                         authorize_live=True, load_local_config=True,
                         repository_root=repository, services=recorder.services(), emit=emit)
    outcome = values[-1]
    assert code == 130 and outcome["published_run_status"] == "complete"
    assert outcome["verification_status"] == "incomplete"
    assert outcome["issue_categories"] == ["operator_interrupted"]
    assert KEY not in json.dumps(values)


def test_unrecognized_pagination_outcome_is_not_emitted(repository):
    recorder = Recorder()
    recorder.verification = RunVerificationReport(
        "run-1", "interrupted", configured_key_check_complete=True,
    )
    recorder.failure = PaginationRunError(
        failure_category=KEY, run_status=KEY, counts=RunCounts(0, 0, 0, 0),
        terminal_failure_category=KEY,
    )
    values, emit = events()
    run_ingestion(run_id="run-1", limit=1, max_pages=1, min_free_bytes=1,
                  authorize_live=True, load_local_config=True,
                  repository_root=repository, services=recorder.services(), emit=emit)
    assert values[-1]["status"] == "interrupted"
    assert values[-1]["issue_categories"] == ["unexpected_failure"]
    assert KEY not in json.dumps(values)


def test_verify_without_config_is_network_free_and_incomplete(repository):
    recorder = Recorder()
    recorder.verification = RunVerificationReport("run-1", "interrupted")
    report = verify_existing_run(run_id="run-1", repository_root=repository,
                                 services=recorder.services())
    assert report.status == "interrupted"
    assert recorder.verify_calls == [(repository / "data/raw", "run-1", None)]
    assert recorder.config_calls == recorder.client_calls == 0


def test_verify_with_explicit_config_passes_key_only_in_memory(repository):
    recorder = Recorder()
    verify_existing_run(run_id="run-1", load_local_config=True,
                        repository_root=repository, services=recorder.services())
    assert recorder.verify_calls[-1][2] == KEY and recorder.client_calls == 0


def test_verify_configuration_failure_is_safe(repository):
    recorder = Recorder(); recorder.config = lambda: (_ for _ in ()).throw(ValueError(KEY))
    report = verify_existing_run(run_id="run-1", load_local_config=True,
                                 repository_root=repository, services=recorder.services())
    assert report.status == "invalid" and report.issue_categories == ("configuration_invalid",)
    assert KEY not in repr(report)


def test_cli_safe_json_and_invalid_arguments(repository, monkeypatch, capsys):
    recorder = Recorder()
    monkeypatch.setattr(operator, "_repository_root", lambda: repository)
    monkeypatch.setattr(operator, "_default_services", recorder.services)
    code = main(["preflight", "--run-id", "run-1", "--limit", "1", "--max-pages", "1",
                 "--min-free-bytes", "1", "--load-local-config"])
    event = json.loads(capsys.readouterr().out)
    assert code == 0 and event["event"] == "preflight_result"
    secret = "raw-secret-argument"
    code = main(["unknown", secret])
    output = capsys.readouterr().out
    assert code == 2 and secret not in output and json.loads(output)["issue_categories"] == ["invalid_command"]


def test_cli_verify_event(repository, monkeypatch, capsys):
    recorder = Recorder()
    recorder.verification = RunVerificationReport("run-1", "interrupted")
    monkeypatch.setattr(operator, "_repository_root", lambda: repository)
    monkeypatch.setattr(operator, "_default_services", recorder.services)
    assert main(["verify", "--run-id", "run-1"]) == 0
    event = json.loads(capsys.readouterr().out)
    assert event["event"] == "verification_result" and event["status"] == "interrupted"


@pytest.mark.parametrize("command,function_name,event_name", [
    (["preflight", "--run-id", "run-1", "--limit", "1", "--max-pages", "1",
      "--min-free-bytes", "1"], "preflight", "preflight_result"),
    (["verify", "--run-id", "run-1"], "verify_existing_run", "verification_result"),
])
def test_cli_interruption_is_one_safe_event(command, function_name, event_name,
                                             monkeypatch, capsys):
    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt(KEY)
    monkeypatch.setattr(operator, function_name, interrupted)
    assert main(command) == 130
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1 and captured.err == "" and KEY not in captured.out
    event = json.loads(lines[0])
    assert event == {"event": event_name, "status": "interrupted",
                     "issue_categories": ["operator_interrupted"]}


def test_no_import_time_configuration_loading(repository):
    recorder = Recorder()
    services = recorder.services()
    assert recorder.config_calls == recorder.client_calls == 0
    assert repr(services) == "OperatorServices()" and KEY not in repr(services)
