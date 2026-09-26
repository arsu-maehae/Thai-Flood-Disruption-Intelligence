"""Self-contained verified HTML report from Phase 4 aggregate outputs."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html import escape
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Sequence

from src.analysis import flood_exposure, temporal_exposure
from src.validation.exposure_outputs import verify_exposure_output, verify_temporal_exposure_output


REPORT_ID = "exploratory-exposure-report-v1-20260927-01"
SCHEMA_VERSION = "1.0"
REPORT_VERSION = "1.0"
OUTPUT_RELATIVE = f"reports/pattani/{REPORT_ID}"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_ERRORS = frozenset({"cleanup_failed", "input_invalid", "invalid_input", "output_exists",
                          "publication_failed", "serialization_failed"})
_EXPECTED_ROADS = (32_358, 4_919, 27_439)
_EXPECTED_HEALTH = (138, 18, 120)


class ExposureReportError(RuntimeError):
    def __init__(self, category: str, *, output_published: bool = False,
                 manifest_published: bool = False, cleanup_failed: bool = False) -> None:
        self.category = category if category in _SAFE_ERRORS else "invalid_input"
        self.output_published = output_published is True
        self.manifest_published = manifest_published is True
        self.cleanup_failed = cleanup_failed is True
        super().__init__(self.category)

    def __repr__(self) -> str:
        return ("ExposureReportError("
                f"category={self.category!r}, output_published={self.output_published!r}, "
                f"manifest_published={self.manifest_published!r}, cleanup_failed={self.cleanup_failed!r})")


@dataclass(frozen=True)
class ExposureReportResult:
    report_id: str
    directory: Path
    html_path: Path
    summary_path: Path
    manifest_path: Path
    descriptors: tuple[tuple[str, int, str], ...]


@dataclass(frozen=True)
class ExposureReportVerification:
    status: str
    issue_categories: tuple[str, ...]
    road_total: int | None
    healthcare_total: int | None

    @property
    def complete(self) -> bool:
        return self.status == "complete" and not self.issue_categories


def publish_exposure_report(
    *, processed_root: Path, generated_at: datetime,
    report_id: str = REPORT_ID,
) -> ExposureReportResult:
    if not isinstance(report_id, str) or not _SAFE_ID.fullmatch(report_id):
        raise ExposureReportError("invalid_input")
    timestamp = _timestamp(generated_at)
    root = Path(processed_root).resolve(); destination = root / "reports/pattani" / report_id
    _contained(root, destination)
    if destination.exists() or destination.is_symlink(): raise ExposureReportError("output_exists")
    data, inputs = _load_inputs(root)
    data = dict(data)
    data["report_id"] = report_id
    try: destination.parent.mkdir(parents=True, exist_ok=True); destination.mkdir(exist_ok=False)
    except FileExistsError: raise ExposureReportError("output_exists") from None
    except OSError: raise ExposureReportError("publication_failed") from None
    published = False
    try:
        html_bytes = _render_html(data).encode("utf-8")
        summary_bytes = _json(data) + b"\n"
        html_path = destination / "index.html"; summary_path = destination / "report_summary.json"
        _publish(html_path, html_bytes); published = True
        _publish(summary_path, summary_bytes)
        descriptors = {html_path.name: _descriptor(html_path), summary_path.name: _descriptor(summary_path)}
        manifest = {"schema_version": SCHEMA_VERSION, "report_version": REPORT_VERSION,
            "report_id": report_id, "status": "complete", "generated_at_utc": timestamp,
            "generation_timestamp_policy": "explicit caller-supplied UTC timestamp",
            "inputs": inputs, "outputs": descriptors,
            "interpretation_scope": "exploratory exposure aggregates only; geometric intersection is not disruption or risk",
            "crs_status": "provider_unverified_exploratory_interpretation",
            "external_dependencies": False}
        manifest_path = destination / "report_manifest.json"
        _publish(manifest_path, _json(manifest) + b"\n", completion=True)
        return ExposureReportResult(report_id, destination, html_path, summary_path, manifest_path,
            tuple((name, int(value["byte_count"]), str(value["sha256"])) for name, value in sorted(descriptors.items())))
    except ExposureReportError: raise
    except Exception: raise ExposureReportError("publication_failed", output_published=published) from None


def verify_exposure_report(processed_root: Path, report_id: str = REPORT_ID) -> ExposureReportVerification:
    issues: set[str] = set(); root = Path(processed_root).resolve()
    if not isinstance(report_id, str) or not _SAFE_ID.fullmatch(report_id):
        return _verification(None, None, {"invalid_input"})
    directory = root / "reports/pattani" / report_id; manifest_path = directory / "report_manifest.json"
    try:
        directory.resolve(strict=True).relative_to(root)
        if directory.is_symlink() or not directory.is_dir() or manifest_path.is_symlink(): raise OSError
        manifest = json.loads(manifest_path.read_bytes(), object_pairs_hook=_reject_duplicates)
    except Exception: return _verification(None, None, {"manifest_invalid"})
    if (manifest.get("status") != "complete" or manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("report_version") != REPORT_VERSION or manifest.get("report_id") != report_id
            or manifest.get("crs_status") != "provider_unverified_exploratory_interpretation"
            or manifest.get("external_dependencies") is not False): issues.add("manifest_invalid")
    expected = {"index.html", "report_summary.json"}; outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or set(outputs) != expected: issues.add("manifest_invalid"); outputs = {}
    try:
        entries = list(directory.iterdir()); allowed = expected | {"report_manifest.json"}
        if any(item.name.startswith(".") or item.name.endswith(".tmp") for item in entries): issues.add("temporary_remnant")
        if any(item.name not in allowed for item in entries): issues.add("unexpected_entry")
    except OSError: issues.add("containment_invalid")
    content: dict[str, bytes] = {}
    for name, descriptor in outputs.items():
        try:
            path = directory / name; path.resolve(strict=True).relative_to(directory.resolve())
            if path.is_symlink() or not path.is_file() or not isinstance(descriptor, dict): raise OSError
            raw = path.read_bytes(); content[name] = raw
            if len(raw) != descriptor.get("byte_count") or hashlib.sha256(raw).hexdigest() != descriptor.get("sha256"):
                issues.add("integrity_failed")
        except Exception: issues.add("integrity_failed")
    roads = health = None
    try:
        summary = json.loads(content["report_summary.json"], object_pairs_hook=_reject_duplicates)
        _validate_summary(summary)
        if summary.get("report_id") != report_id: raise ValueError
        roads = summary["headline"]["roads"]["total"]
        health = summary["headline"]["healthcare"]["total"]
        html = content["index.html"].decode("utf-8", errors="strict").casefold()
        prohibited = ("http://", "https://", "<script src", "<iframe", "<img", "@import", "url(",
                      "official crs verified", "disruption score", "risk score")
        if any(token in html for token in prohibited): issues.add("html_unsafe")
        if ("<svg" not in html or "<table" not in html or "exploratory / crs not officially verified" not in html
                or f"{roads:,}" not in html or f"{health:,}" not in html): issues.add("html_invalid")
    except Exception: issues.add("summary_invalid")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {"phase4a", "phase4b"}: issues.add("lineage_invalid")
    else:
        for value in inputs.values():
            try:
                path_value = value["manifest_relative_path"]
                if not _relative(path_value) or value.get("verified") is not True: raise ValueError
                path = root / PurePosixPath(path_value); raw = path.read_bytes(); path.resolve(strict=True).relative_to(root)
                if hashlib.sha256(raw).hexdigest() != value.get("manifest_sha256"): raise ValueError
            except Exception: issues.add("lineage_invalid")
    try:
        if not verify_exposure_output(root, flood_exposure.OUTPUT_RELATIVE).complete:
            issues.add("lineage_invalid")
        if not verify_temporal_exposure_output(root, temporal_exposure.OUTPUT_RELATIVE).complete:
            issues.add("lineage_invalid")
    except Exception:
        issues.add("lineage_invalid")
    return _verification(roads, health, issues)


def _load_inputs(root: Path) -> tuple[dict[str, object], dict[str, object]]:
    phase4a = verify_exposure_output(root, flood_exposure.OUTPUT_RELATIVE)
    phase4b = verify_temporal_exposure_output(root, temporal_exposure.OUTPUT_RELATIVE)
    if not phase4a.complete or not phase4b.complete: raise ExposureReportError("input_invalid")
    a_dir = root / flood_exposure.OUTPUT_RELATIVE; b_dir = root / temporal_exposure.OUTPUT_RELATIVE
    try:
        a_summary = json.loads((a_dir / "exposure_summary.json").read_bytes(), object_pairs_hook=_reject_duplicates)
        b_summary = json.loads((b_dir / "annual_exposure_summary.json").read_bytes(), object_pairs_hook=_reject_duplicates)
        frequency = json.loads((b_dir / "frequency_consistency.json").read_bytes(), object_pairs_hook=_reject_duplicates)
        categories = list(csv.DictReader((b_dir / "road_category_annual_exposure.csv").open(encoding="utf-8", newline="")))
    except Exception: raise ExposureReportError("input_invalid") from None
    years = tuple(f"y_{year}" for year in range(2011, 2025))
    data: dict[str, object] = {"report_id": REPORT_ID,
        "policy_label": "exploratory_non_authoritative",
        "crs_status": "provider_unverified_exploratory_interpretation",
        "analysis_ids": {"phase4a": flood_exposure.ANALYSIS_ID, "phase4b": temporal_exposure.ANALYSIS_ID},
        "headline": {"roads": {"total": a_summary["roads"]["total"], "exposed": a_summary["roads"]["exposed"], "non_exposed": a_summary["roads"]["non_exposed"]},
                     "healthcare": {"total": a_summary["healthcare"]["total"], "exposed": a_summary["healthcare"]["exposed"], "non_exposed": a_summary["healthcare"]["non_exposed"]}},
        "years": [int(year[2:]) for year in years],
        "annual_road_exposed": [b_summary["annual_road_exposed"][year] for year in years],
        "annual_healthcare_exposed": [b_summary["annual_healthcare_exposed"][year] for year in years],
        "road_categories": [{"category": row["highway_category"], "ever_exposed": int(row["ever_exposed"]),
                             "total_records": int(row["total_records"])} for row in categories],
        "frequency_consistency": {"feature_count": frequency["feature_count"], "match_count": frequency["match_count"],
            "mismatch_count": frequency["mismatch_count"], "missing_or_invalid_count": frequency["missing_or_invalid_count"],
            "relationship_status": "observed_structural_relationship_only"},
        "caveats": ["Geometric intersection only", "Not a disruption, risk, severity, accessibility, prediction, emergency-response, or completeness product",
                    "Provider CRS remains officially unverified", "Annual counts are not unique totals across years",
                    "Observed highway categories do not establish drivability"]}
    _validate_summary(data)
    inputs = {"phase4a": _input(a_dir / "analysis_manifest.json", root, flood_exposure.ANALYSIS_ID),
              "phase4b": _input(b_dir / "analysis_manifest.json", root, temporal_exposure.ANALYSIS_ID)}
    return data, inputs


def _validate_summary(data: object) -> None:
    try:
        if not isinstance(data, dict) or data.get("policy_label") != "exploratory_non_authoritative": raise ValueError
        headline = data["headline"]; roads = headline["roads"]; health = headline["healthcare"]
        for group, expected in ((roads, _EXPECTED_ROADS), (health, _EXPECTED_HEALTH)):
            values = (group["total"], group["exposed"], group["non_exposed"])
            if any(type(value) is not int or value < 0 for value in values) or values != expected or values[0] != values[1] + values[2]: raise ValueError
        years = data["years"]; road_annual = data["annual_road_exposed"]; health_annual = data["annual_healthcare_exposed"]
        if years != list(range(2011, 2025)) or len(road_annual) != 14 or len(health_annual) != 14: raise ValueError
        if any(type(value) is not int or value < 0 for value in [*road_annual, *health_annual]): raise ValueError
        frequency = data["frequency_consistency"]
        values = [frequency[key] for key in ("feature_count", "match_count", "mismatch_count", "missing_or_invalid_count")]
        if any(type(value) is not int or value < 0 for value in values) or values != [112073, 112073, 0, 0]: raise ValueError
        categories = data["road_categories"]
        if not isinstance(categories, list) or [item["category"] for item in categories] != sorted(item["category"] for item in categories): raise ValueError
        for item in categories:
            if (not isinstance(item["category"], str) or not item["category"]
                    or type(item["ever_exposed"]) is not int or type(item["total_records"]) is not int
                    or not 0 <= item["ever_exposed"] <= item["total_records"]): raise ValueError
        if sum(item["ever_exposed"] for item in categories) != roads["exposed"] or sum(item["total_records"] for item in categories) != roads["total"]: raise ValueError
    except Exception: raise ExposureReportError("input_invalid") from None


def _render_html(data: dict[str, object]) -> str:
    years = data["years"]; roads = data["annual_road_exposed"]; health = data["annual_healthcare_exposed"]
    categories = data["road_categories"]; headline = data["headline"]; frequency = data["frequency_consistency"]
    def chart(title: str, values: Sequence[int], color: str) -> str:
        maximum = max(values) or 1; bars = []
        for index, value in enumerate(values):
            height = round(160 * value / maximum, 3); x = 28 + index * 42; y = 190 - height
            bars.append(f'<rect x="{x}" y="{y}" width="24" height="{height}" fill="{color}"><title>{escape(str(years[index]))}: {value}</title></rect>')
            bars.append(f'<text x="{x + 12}" y="208" text-anchor="middle">{escape(str(years[index]))}</text>')
        return f'<figure><figcaption>{escape(title)}</figcaption><svg role="img" aria-label="{escape(title)}" viewBox="0 0 640 225">{"".join(bars)}</svg></figure>'
    def annual_table(title: str, values: Sequence[int]) -> str:
        rows = "".join(f"<tr><th scope=\"row\">{year}</th><td>{value:,}</td></tr>" for year, value in zip(years, values))
        return f'<table><caption>{escape(title)}</caption><thead><tr><th>Year</th><th>Exposed count</th></tr></thead><tbody>{rows}</tbody></table>'
    category_rows = "".join(f'<tr><th scope="row">{escape(item["category"])}</th><td>{item["ever_exposed"]:,}</td><td>{item["total_records"]:,}</td></tr>' for item in categories)
    return "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Pattani Exploratory Flood Exposure</title><style>" + _CSS + "</style></head><body><main>" + \
        '<header><p class="banner">Exploratory / CRS not officially verified</p><h1>Pattani Flood Exposure — Offline Aggregate Report</h1><p>Verified Phase 4A and 4B aggregates. Geometric intersection only.</p></header>' + \
        f'<section aria-labelledby="headline"><h2 id="headline">Headline totals</h2><div class="cards"><article><h3>Road segments</h3><strong>{headline["roads"]["total"]:,}</strong><p>{headline["roads"]["exposed"]:,} exposed · {headline["roads"]["non_exposed"]:,} non-exposed</p></article><article><h3>Healthcare candidates</h3><strong>{headline["healthcare"]["total"]:,}</strong><p>{headline["healthcare"]["exposed"]:,} exposed · {headline["healthcare"]["non_exposed"]:,} non-exposed</p></article></div></section>' + \
        f'<section><h2>Annual exposure</h2>{chart("Annual exposed road segments", roads, "#176b87")}{annual_table("Annual exposed road segments", roads)}{chart("Annual exposed healthcare candidates", health, "#8a4f7d")}{annual_table("Annual exposed healthcare candidates", health)}</section>' + \
        f'<section><h2>Observed road categories</h2><p>Labels are observed source categories and do not establish drivability or importance.</p><table><caption>Ever-exposed segments by observed category</caption><thead><tr><th>Category</th><th>Ever exposed</th><th>Total segments</th></tr></thead><tbody>{category_rows}</tbody></table></section>' + \
        f'<section><h2>Frequency consistency</h2><p>{frequency["match_count"]:,} of {frequency["feature_count"]:,} features matched the observed structural equality; mismatches: {frequency["mismatch_count"]:,}; missing/invalid: {frequency["missing_or_invalid_count"]:,}.</p><p>This observation does not define the meaning of any field.</p></section>' + \
        '<section><h2>Interpretation and limitations</h2><ul><li>“Exposed” means exact geometric intersection with at least one flood polygon.</li><li>Annual counts must not be summed as unique infrastructure totals.</li><li>This is not a disruption, risk, severity, accessibility, prediction, emergency-response, or completeness product.</li><li>No map is shown because official GISTDA and DGA CRS remain unresolved.</li></ul></section>' + \
        f'<footer><p>Provenance: {escape(data["analysis_ids"]["phase4a"])} and {escape(data["analysis_ids"]["phase4b"])}</p></footer></main></body></html>\n'


_CSS = """*{box-sizing:border-box}body{margin:0;background:#f4f7f8;color:#17242b;font-family:system-ui,sans-serif;line-height:1.5}main{max-width:1100px;margin:auto;padding:2rem}h1,h2,h3{line-height:1.2}.banner{background:#69324f;color:#fff;padding:.8rem 1rem;font-weight:700;border-radius:.4rem}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:1rem}.cards article,section{background:#fff;padding:1rem;margin:1rem 0;border:1px solid #ccd8dc;border-radius:.5rem}.cards strong{font-size:2rem}figure{margin:1rem 0;overflow-x:auto}figcaption,caption{font-weight:700;text-align:left;padding:.5rem 0}svg{min-width:640px;width:100%;height:auto;border-bottom:1px solid #53656d}svg text{font-size:10px;fill:#17242b}table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #aab9bf;padding:.45rem;text-align:right}th:first-child{text-align:left}thead{background:#e4eef1}@media print{body{background:#fff}section,.cards article{break-inside:avoid;border-color:#777}main{max-width:none;padding:0}.banner{color:#000;background:#ddd}}"""


def _input(path: Path, root: Path, analysis_id: str) -> dict[str, object]:
    raw = path.read_bytes(); manifest = json.loads(raw, object_pairs_hook=_reject_duplicates)
    if manifest.get("analysis_id") != analysis_id or manifest.get("status") != "complete": raise ExposureReportError("input_invalid")
    return {"analysis_id": analysis_id, "manifest_relative_path": path.relative_to(root).as_posix(),
            "manifest_sha256": hashlib.sha256(raw).hexdigest(), "verified": True}


def _descriptor(path: Path) -> dict[str, object]:
    raw = path.read_bytes(); return {"relative_path": path.name, "byte_count": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _json(value: object) -> bytes:
    try: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except Exception: raise ExposureReportError("serialization_failed") from None


def _publish(path: Path, content: bytes, *, completion: bool = False) -> None:
    temporary: Path | None = None; linked = False
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name); handle.write(content); handle.flush(); os.fsync(handle.fileno())
        os.link(temporary, path); linked = True; temporary.unlink(); temporary = None
    except FileExistsError:
        failed = not _cleanup(temporary)
        raise ExposureReportError("cleanup_failed" if failed else "output_exists", output_published=linked,
                                  manifest_published=linked and completion, cleanup_failed=failed) from None
    except OSError:
        failed = not _cleanup(temporary)
        raise ExposureReportError("cleanup_failed" if linked else "publication_failed", output_published=linked,
                                  manifest_published=linked and completion, cleanup_failed=failed) from None


def _cleanup(path: Path | None) -> bool:
    if path is None: return True
    try: path.unlink(missing_ok=True); return True
    except OSError: return False


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None: raise ExposureReportError("invalid_input")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _contained(root: Path, path: Path) -> None:
    try: path.resolve(strict=False).relative_to(root)
    except Exception: raise ExposureReportError("invalid_input") from None


def _relative(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value: return False
    path = PurePosixPath(value); return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _verification(roads: int | None, health: int | None, issues: set[str]) -> ExposureReportVerification:
    safe = tuple(sorted(issues)); return ExposureReportVerification("complete" if not safe else "invalid", safe, roads, health)


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result: raise ValueError
        result[key] = value
    return result
