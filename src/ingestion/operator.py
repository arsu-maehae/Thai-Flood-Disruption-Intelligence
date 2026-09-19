"""Guarded Phase 1 operator for offline checks and separately authorized runs.

``preflight`` and ``verify`` never construct an HTTP client. Configuration is
loaded only when explicitly requested. A ``run`` repeats preflight immediately
before client construction; an earlier result is never an authorization token.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Final, Mapping, Sequence

from src.configuration import GistdaConfig, OFFICIAL_GISTDA_API_BASE_URL, load_config
from src.ingestion.flood_frequency import PATTANI_PROVINCE_ID
from src.ingestion.gistda_client import GistdaClient
from src.ingestion.pagination import (
    MAX_JOURNALED_PAGES, PaginationRunError, PaginationRunResult, paginate_pattani,
)
from src.ingestion.run_manifest import RUNS_SUBDIRECTORY, RunCounts
from src.ingestion.run_verification import RunVerificationReport, verify_run

CANONICAL_OUTPUT_RELATIVE: Final = Path("data/raw")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in range(1, 10)
}
_RUN_FAILURE_CATEGORIES = frozenset({
    "journal_start_failed", "journal_page_cleanup_failed", "journal_page_failed",
    "completion_cleanup_failed", "completion_terminal_failed", "request_timeout",
    "request_connection_failed", "request_http_failed", "source_sanitization_failed",
    "source_structure_invalid", "source_publication_failed", "page_count_invalid",
    "repeated_page", "duplicate_feature_id", "number_matched_changed",
    "max_pages_exhausted", "unexpected_failure",
})
_TERMINAL_FAILURE_CATEGORIES = frozenset({
    "failure_terminal_cleanup_failed", "failure_terminal_failed",
})


@dataclass(frozen=True)
class GitInspection:
    clean: bool
    env_ignored: bool
    output_ignored: bool
    output_untracked: bool
    env_untracked: bool = True


@dataclass(frozen=True, repr=False)
class OperatorServices:
    """Injectable boundaries; secrets and callable internals stay out of repr."""

    config_loader: Callable[[], GistdaConfig] = field(repr=False)
    client_factory: Callable[[GistdaConfig], object] = field(repr=False)
    paginator: Callable[..., PaginationRunResult] = field(repr=False)
    verifier: Callable[..., RunVerificationReport] = field(repr=False)
    git_inspector: Callable[[Path, str], GitInspection] = field(repr=False)
    disk_usage: Callable[[Path], object] = field(repr=False)
    clock: Callable[[], datetime] = field(repr=False)
    probe_token: Callable[[], str] = field(repr=False)

    def __repr__(self) -> str:
        return "OperatorServices()"


@dataclass(frozen=True)
class PreflightReport:
    run_id: str | None
    ready: bool
    issue_categories: tuple[str, ...]
    limit: int | None
    max_pages: int | None
    min_free_bytes: int | None
    measured_free_bytes: int | None
    configuration_loaded: bool
    git_dirty_acknowledged: bool
    output_root: str = "data/raw"

    def to_dict(self) -> dict[str, object]:
        return {
            "event": "preflight_result", "run_id": self.run_id,
            "ready": self.ready, "issue_categories": list(self.issue_categories),
            "limit": self.limit, "max_pages": self.max_pages,
            "min_free_bytes": self.min_free_bytes,
            "measured_free_bytes": self.measured_free_bytes,
            "configuration_loaded": self.configuration_loaded,
            "git_dirty_acknowledged": self.git_dirty_acknowledged,
            "output_root": self.output_root,
        }


def _default_services() -> OperatorServices:
    return OperatorServices(
        config_loader=load_config,
        client_factory=GistdaClient,
        paginator=paginate_pattani,
        verifier=verify_run,
        git_inspector=_inspect_git,
        disk_usage=shutil.disk_usage,
        clock=lambda: datetime.now(timezone.utc),
        probe_token=lambda: uuid.uuid4().hex,
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _valid_run_id(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) <= 128
        and _SAFE_ID.fullmatch(value) is not None and not value.endswith(".")
        and value.split(".")[0].upper() not in _WINDOWS_RESERVED
    )


def _positive(value: object, maximum: int | None = None) -> bool:
    return (
        type(value) is int and value > 0
        and (maximum is None or value <= maximum)
    )


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _canonical_root(repository_root: str | Path) -> tuple[Path | None, str | None]:
    try:
        repository = Path(repository_root).absolute()
        output = repository / CANONICAL_OUTPUT_RELATIVE
        if not repository.exists() or not repository.is_dir():
            return None, "repository_root_invalid"
        for path in (repository, repository / "data", output):
            if not path.exists() or not path.is_dir():
                return None, "output_root_unavailable"
            if _is_link(path):
                return None, "output_root_symlink_rejected"
        resolved_repository = repository.resolve()
        resolved_output = output.resolve()
        if resolved_output != resolved_repository / CANONICAL_OUTPUT_RELATIVE:
            return None, "output_root_escape_rejected"
        return resolved_output, None
    except (OSError, TypeError, ValueError):
        return None, "output_root_resolution_failed"


def _git(arguments: Sequence[str], repository: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=repository, text=True, capture_output=True,
        check=False, timeout=15,
    )


def _inspect_git(repository: Path, run_id: str) -> GitInspection:
    status = _git(("status", "--porcelain", "--untracked-files=all"), repository)
    env = _git(("check-ignore", "-q", "--", ".env"), repository)
    env_tracked = _git(("ls-files", "--error-unmatch", "--", ".env"), repository)
    source = CANONICAL_OUTPUT_RELATIVE / "gistda/flood_freq/pattani"
    journal = source / "runs" / run_id
    prospective = tuple(path.as_posix() for path in (
        source / "prospective.sanitized.json",
        source / "prospective.metadata.json",
        journal / "run_started.json",
        journal / "page_000000.json",
        journal / "run_complete.json",
        journal / "run_failed.json",
        source / ".prospective.sanitized.json.operator-probe.tmp",
        journal / ".page_000000.json.operator-probe.tmp",
    ))
    ignored_results = tuple(
        _git(("check-ignore", "-q", "--", path), repository) for path in prospective
    )
    tracked_results = tuple(
        _git(("ls-files", "--error-unmatch", "--", path), repository) for path in prospective
    )
    if (
        status.returncode != 0 or env.returncode not in {0, 1}
        or env_tracked.returncode not in {0, 1}
        or any(result.returncode not in {0, 1} for result in ignored_results)
        or any(result.returncode not in {0, 1} for result in tracked_results)
    ):
        raise RuntimeError("fixed git inspection failure")
    return GitInspection(not bool(status.stdout), env.returncode == 0,
                         all(result.returncode == 0 for result in ignored_results),
                         all(result.returncode == 1 for result in tracked_results),
                         env_tracked.returncode == 1)


def _probe(output_root: Path, token: str) -> tuple[str, ...]:
    """Exercise write/fsync/link using only a newly owned disposable directory."""
    class _ProbeStopped(Exception):
        pass

    issues: list[str] = []
    directory = output_root / f".operator-preflight-{token}"
    source = directory / "source.probe"
    linked = directory / "linked.probe"
    owned = False
    try:
        directory.mkdir(exist_ok=False)
        owned = True
        try:
            with source.open("xb") as stream:
                stream.write(b"operator-preflight\n")
                try:
                    stream.flush()
                except OSError:
                    issues.append("probe_flush_failed")
                    raise _ProbeStopped from None
                try:
                    os.fsync(stream.fileno())
                except OSError:
                    issues.append("probe_fsync_failed")
                    raise _ProbeStopped from None
        except OSError:
            issues.append("probe_write_failed")
            raise _ProbeStopped from None
        try:
            os.link(source, linked)
        except OSError:
            issues.append("probe_hardlink_failed")
    except FileExistsError:
        issues.append("probe_collision")
    except _ProbeStopped:
        pass
    except OSError:
        issues.append("probe_directory_failed")
    finally:
        if owned:
            cleanup_failed = False
            for path in (linked, source):
                try:
                    if os.path.lexists(path):
                        path.unlink()
                except OSError:
                    cleanup_failed = True
            try:
                directory.rmdir()
            except OSError:
                cleanup_failed = True
            if cleanup_failed:
                issues.append("probe_cleanup_failed")
    return tuple(issues)


def preflight(
    *, run_id: str, limit: int, max_pages: int, min_free_bytes: int,
    load_local_config: bool = False, acknowledge_dirty: bool = False,
    repository_root: str | Path | None = None,
    services: OperatorServices | None = None,
) -> tuple[PreflightReport, GistdaConfig | None]:
    """Perform fresh readiness checks; returned config is memory-only."""
    services = services or _default_services()
    issues: list[str] = []
    safe_run_id = run_id if _valid_run_id(run_id) else None
    if safe_run_id is None:
        issues.append("invalid_run_id")
    valid_limit = type(limit) is int and 1 <= limit <= 10000
    valid_pages = _positive(max_pages, MAX_JOURNALED_PAGES)
    valid_floor = _positive(min_free_bytes)
    if not valid_limit:
        issues.append("invalid_limit")
    if not valid_pages:
        issues.append("invalid_max_pages")
    if not valid_floor:
        issues.append("invalid_min_free_bytes")

    repository = Path(repository_root) if repository_root is not None else _repository_root()
    output_root, root_issue = _canonical_root(repository)
    if root_issue:
        issues.append(root_issue)

    config = None
    if not load_local_config:
        issues.append("configuration_incomplete")
    else:
        try:
            config = services.config_loader()
            if config.province_id != PATTANI_PROVINCE_ID:
                issues.append("province_scope_mismatch")
            if config.api_base_url != OFFICIAL_GISTDA_API_BASE_URL:
                issues.append("base_url_mismatch")
            if safe_run_id is not None and config.api_key in safe_run_id:
                safe_run_id = None
                issues.append("credential_in_run_id")
        except Exception:
            issues.append("configuration_invalid")

    if safe_run_id is not None and output_root is not None:
        try:
            inspection = services.git_inspector(repository.resolve(), safe_run_id)
            if not inspection.clean and not acknowledge_dirty:
                issues.append("git_state_dirty")
            if not inspection.env_ignored:
                issues.append("env_not_ignored")
            if not inspection.env_untracked:
                issues.append("env_tracked")
            if not inspection.output_ignored:
                issues.append("prospective_output_not_ignored")
            if not inspection.output_untracked:
                issues.append("prospective_output_tracked")
        except Exception:
            issues.append("git_inspection_failed")
        run_directory = output_root / RUNS_SUBDIRECTORY / safe_run_id
        try:
            if os.path.lexists(run_directory):
                issues.append("run_id_already_exists")
        except OSError:
            issues.append("run_destination_check_failed")

    measured_free = None
    if output_root is not None:
        try:
            measured_free = services.disk_usage(output_root).free
            if type(measured_free) is not int or measured_free < 0:
                raise ValueError
            if valid_floor and measured_free < min_free_bytes:
                issues.append("insufficient_free_space")
        except Exception:
            measured_free = None
            issues.append("disk_measurement_failed")
        try:
            token = services.probe_token()
            if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9]+", token) is None:
                raise ValueError
            issues.extend(_probe(output_root, token))
        except Exception:
            issues.append("probe_setup_failed")

    issue_tuple = tuple(sorted(set(issues)))
    return PreflightReport(
        safe_run_id, not issue_tuple, issue_tuple,
        limit if valid_limit else None, max_pages if valid_pages else None,
        min_free_bytes if valid_floor else None, measured_free,
        config is not None, acknowledge_dirty,
    ), config


def _relative(path: Path | None, root: Path) -> str | None:
    if path is None:
        return None
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (OSError, ValueError, TypeError):
        return None


def _count_dict(counts: RunCounts | None) -> Mapping[str, int | None] | None:
    return counts.to_dict() if isinstance(counts, RunCounts) else None


def _verification_fields(
    report: RunVerificationReport | None, api_key: str,
) -> dict[str, object]:
    """Reduce verifier output to explicit safe coverage fields."""
    if not isinstance(report, RunVerificationReport):
        return {
            "verification_status": "incomplete",
            "verification_issue_categories": [],
            "configured_key_check_complete": False,
            "counts_final": False,
        }
    status = report.status if report.status in {
        "not_found", "invalid", "interrupted", "complete", "failed"
    } else "invalid"
    issues = [
        issue for issue in report.issue_categories
        if isinstance(issue, str) and re.fullmatch(r"[a-z][a-z0-9_]*", issue)
        and (not api_key or api_key not in issue)
    ]
    return {
        "verification_status": status,
        "verification_issue_categories": issues,
        "configured_key_check_complete": report.configured_key_check_complete is True,
        "counts_final": report.counts_final is True,
    }


def _incomplete_verification() -> dict[str, object]:
    return _verification_fields(None, "")


def _verification_matches_published(fields: Mapping[str, object], status: str) -> bool:
    expected = status if status in {"complete", "failed", "interrupted"} else "not_found"
    return (
        fields["verification_status"] == expected
        and (
            fields["configured_key_check_complete"] is True
            or expected == "not_found"
        )
    )


def run_ingestion(
    *, run_id: str, limit: int, max_pages: int, min_free_bytes: int,
    authorize_live: bool, load_local_config: bool,
    acknowledge_dirty: bool = False, repository_root: str | Path | None = None,
    services: OperatorServices | None = None,
    emit: Callable[[Mapping[str, object]], None] | None = None,
) -> int:
    """Repeat preflight, then perform one separately authorized journaled run."""
    services = services or _default_services()
    output = emit or _emit
    if not authorize_live or not load_local_config:
        categories = []
        if not authorize_live:
            categories.append("live_authorization_flag_required")
        if not load_local_config:
            categories.append("configuration_loading_flag_required")
        output({"event": "final_run_outcome", "run_id": run_id if _valid_run_id(run_id) else None,
                "status": "refused", "issue_categories": categories})
        return 2
    try:
        report, config = preflight(
            run_id=run_id, limit=limit, max_pages=max_pages,
            min_free_bytes=min_free_bytes, load_local_config=True,
            acknowledge_dirty=acknowledge_dirty, repository_root=repository_root,
            services=services,
        )
    except KeyboardInterrupt:
        output({"event": "final_run_outcome",
                "run_id": run_id if _valid_run_id(run_id) else None,
                "status": "interrupted", "issue_categories": ["operator_interrupted"]})
        return 130
    output(report.to_dict())
    if not report.ready or config is None:
        output({"event": "final_run_outcome", "run_id": report.run_id,
                "status": "refused", "issue_categories": list(report.issue_categories)})
        return 2
    repository = Path(repository_root) if repository_root is not None else _repository_root()
    output_root = (repository.absolute() / CANONICAL_OUTPUT_RELATIVE).resolve()
    output({"event": "run_start", "run_id": report.run_id, "status": "starting",
            "limit": limit, "max_pages": max_pages, "output_root": "data/raw"})
    client = None
    try:
        client = services.client_factory(config)
        output({"event": "dispatch", "run_id": report.run_id, "status": "dispatching",
                "requested_offset": 0, "limit": limit})
        result = services.paginator(
            config=config, client=client, output_root=output_root, limit=limit,
            max_pages=max_pages, retrieved_at=None, run_id=run_id, clock=services.clock,
        )
        offsets = (
            list(result.requested_offsets)
            if all(type(value) is int and value >= 0 for value in result.requested_offsets)
            else []
        )
        stop_reason = result.stop_reason if result.stop_reason in {
            "empty_page", "partial_page"
        } else "unknown"
        try:
            verified = services.verifier(output_root, run_id, api_key=config.api_key)
        except KeyboardInterrupt:
            output({
                "event": "final_run_outcome", "run_id": report.run_id,
                "status": "complete", "published_run_status": "complete",
                "issue_categories": ["operator_interrupted"],
                **_incomplete_verification(),
                "counts": _count_dict(result.counts),
                "terminal_path": _relative(result.terminal_path, output_root),
            })
            return 130
        except Exception:
            verified = None
        verification = _verification_fields(verified, config.api_key)
        verification_ok = (
            verification["verification_status"] == "complete"
            and verification["configured_key_check_complete"] is True
        )
        output({
            "event": "final_run_outcome", "run_id": report.run_id,
            "status": "complete", "published_run_status": "complete",
            "issue_categories": [] if verification_ok else ["post_run_verification_failed"],
            **verification, "stop_reason": stop_reason,
            "requested_offsets": offsets,
            "total_features": result.total_features
            if type(result.total_features) is int and result.total_features >= 0 else None,
            "counts": _count_dict(result.counts),
            "terminal_path": _relative(result.terminal_path, output_root),
        })
        return 0 if verification_ok else 1
    except KeyboardInterrupt:
        verified = None
        try:
            verified = services.verifier(output_root, run_id, api_key=config.api_key)
        except KeyboardInterrupt:
            verified = None
        except Exception:
            verified = None
        verification = _verification_fields(verified, config.api_key)
        observed_status = (
            verification["verification_status"]
            if verification["verification_status"] in {"complete", "failed"}
            else "interrupted"
        )
        output({"event": "final_run_outcome", "run_id": report.run_id,
                "status": observed_status,
                "published_run_status": observed_status
                if observed_status in {"complete", "failed"} else None,
                "issue_categories": ["operator_interrupted"], **verification})
        return 130
    except PaginationRunError as error:
        category = (error.failure_category if error.failure_category in _RUN_FAILURE_CATEGORIES
                    else "unexpected_failure")
        terminal_category = (
            error.terminal_failure_category
            if error.terminal_failure_category in _TERMINAL_FAILURE_CATEGORIES else None
        )
        status = error.run_status if error.run_status in {
            "not_started", "interrupted", "failed", "complete"
        } else "interrupted"
        try:
            verified = services.verifier(output_root, run_id, api_key=config.api_key)
        except KeyboardInterrupt:
            output({
                "event": "final_run_outcome", "run_id": report.run_id,
                "status": status,
                "published_run_status": status if status in {"complete", "failed"} else None,
                "issue_categories": [category, "operator_interrupted"],
                **_incomplete_verification(), "counts": _count_dict(error.counts),
                "record_published": error.record_published,
                "cleanup_failed": error.cleanup_failed,
            })
            return 130
        except Exception:
            verified = None
        verification = _verification_fields(verified, config.api_key)
        verification_ok = _verification_matches_published(verification, status)
        categories = [category] + ([terminal_category] if terminal_category else [])
        if not verification_ok:
            categories.append("post_run_verification_failed")
        output({
            "event": "final_run_outcome", "run_id": report.run_id,
            "status": status,
            "published_run_status": status if status in {"complete", "failed"} else None,
            "issue_categories": categories, **verification,
            "counts": _count_dict(error.counts),
            "terminal_path": _relative(error.terminal_path, output_root),
            "record_published": error.record_published,
            "cleanup_failed": error.cleanup_failed,
        })
        return 1
    except Exception:
        output({"event": "final_run_outcome", "run_id": report.run_id,
                "status": "interrupted", "issue_categories": ["operator_run_failed"]})
        return 1
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def verify_existing_run(
    *, run_id: str, load_local_config: bool = False,
    repository_root: str | Path | None = None,
    services: OperatorServices | None = None,
) -> RunVerificationReport:
    """Call the existing read-only verifier; never construct a client."""
    services = services or _default_services()
    repository = Path(repository_root) if repository_root is not None else _repository_root()
    output_root, issue = _canonical_root(repository)
    if issue or output_root is None:
        return RunVerificationReport(None, "invalid", issue_categories=(issue or "output_root_invalid",))
    key = None
    if load_local_config:
        try:
            key = services.config_loader().api_key
        except Exception:
            return RunVerificationReport(
                run_id if _valid_run_id(run_id) else None, "invalid",
                issue_categories=("configuration_invalid",),
            )
    return services.verifier(output_root, run_id, api_key=key)


def _emit(event: Mapping[str, object]) -> None:
    print(json.dumps(dict(event), sort_keys=True, separators=(",", ":")))


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise ValueError("invalid_command")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m src.ingestion.operator", add_help=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name in ("preflight", "run"):
        command = commands.add_parser(name, add_help=False)
        command.add_argument("--run-id", required=True)
        command.add_argument("--limit", required=True, type=int)
        command.add_argument("--max-pages", required=True, type=int)
        command.add_argument("--min-free-bytes", required=True, type=int)
        command.add_argument("--load-local-config", action="store_true")
        command.add_argument("--acknowledge-dirty", action="store_true")
        if name == "run":
            command.add_argument("--authorize-live", action="store_true")
    verify = commands.add_parser("verify", add_help=False)
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--load-local-config", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
    except (SystemExit, ValueError) as error:
        if isinstance(error, SystemExit) and error.code == 0:
            return 0
        _emit({"event": "operator_error", "status": "refused",
               "issue_categories": ["invalid_command"]})
        return 2
    if arguments.command == "preflight":
        try:
            report, _ = preflight(
                run_id=arguments.run_id, limit=arguments.limit,
                max_pages=arguments.max_pages, min_free_bytes=arguments.min_free_bytes,
                load_local_config=arguments.load_local_config,
                acknowledge_dirty=arguments.acknowledge_dirty,
            )
        except KeyboardInterrupt:
            _emit({"event": "preflight_result", "status": "interrupted",
                   "issue_categories": ["operator_interrupted"]})
            return 130
        _emit(report.to_dict())
        return 0 if report.ready else 2
    if arguments.command == "verify":
        try:
            report = verify_existing_run(run_id=arguments.run_id,
                                         load_local_config=arguments.load_local_config)
        except KeyboardInterrupt:
            _emit({"event": "verification_result", "status": "interrupted",
                   "issue_categories": ["operator_interrupted"]})
            return 130
        _emit({"event": "verification_result", **report.to_dict()})
        return 0 if report.status in {"complete", "failed", "interrupted"} else 2
    return run_ingestion(
        run_id=arguments.run_id, limit=arguments.limit, max_pages=arguments.max_pages,
        min_free_bytes=arguments.min_free_bytes,
        authorize_live=arguments.authorize_live,
        load_local_config=arguments.load_local_config,
        acknowledge_dirty=arguments.acknowledge_dirty,
    )


if __name__ == "__main__":
    raise SystemExit(main())
