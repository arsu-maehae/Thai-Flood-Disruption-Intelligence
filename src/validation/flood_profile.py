"""Offline structural aggregates for already validated flood-frequency pages."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from .flood_frequency import (
    PageValidationResult,
    ParsedSourcePage,
    SourceValidationError,
    _validated_features,
    validate_page_sequence,
)


PROFILE_SCHEMA_VERSION = "1.0"
_GEOMETRY_TYPES = frozenset({
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
})
_SAFE_ERROR_CATEGORIES = frozenset({
    "invalid_profile_input",
    "invalid_validated_page",
    "nonfinite_number",
    "profile_invariant_failed",
    "validation_failed",
})
_MISSING = object()


class FloodProfileError(ValueError):
    """Fixed-category profiler error that never retains source values."""

    def __init__(self, category: str) -> None:
        safe = category if category in _SAFE_ERROR_CATEGORIES else "validation_failed"
        self.category = safe
        super().__init__(safe)


@dataclass(frozen=True, repr=False)
class ValidatedProfilePage:
    """Pair of sealed Phase 2B outputs; source content is excluded from repr."""

    parsed: ParsedSourcePage = field(repr=False)
    validation: PageValidationResult = field(repr=False)

    def __repr__(self) -> str:
        return "ValidatedProfilePage()"


@dataclass(frozen=True)
class MemberCounts:
    missing: int
    null: int
    object: int
    other: int

    @property
    def total(self) -> int:
        return self.missing + self.null + self.object + self.other

    def to_dict(self) -> dict[str, int]:
        return {
            "missing": self.missing,
            "null": self.null,
            "object": self.object,
            "other": self.other,
        }


@dataclass(frozen=True)
class JsonTypeCounts:
    null: int = 0
    boolean: int = 0
    integer: int = 0
    number: int = 0
    string: int = 0
    array: int = 0
    object: int = 0

    @property
    def total(self) -> int:
        return sum((
            self.null,
            self.boolean,
            self.integer,
            self.number,
            self.string,
            self.array,
            self.object,
        ))

    def to_dict(self) -> dict[str, int]:
        return {
            "null": self.null,
            "boolean": self.boolean,
            "integer": self.integer,
            "number": self.number,
            "string": self.string,
            "array": self.array,
            "object": self.object,
        }


@dataclass(frozen=True)
class GeometryTypeCount:
    geometry_type: str
    count: int

    def to_dict(self) -> dict[str, object]:
        return {"geometry_type": self.geometry_type, "count": self.count}


@dataclass(frozen=True)
class PropertyFieldProfile:
    field_name: str
    present_count: int
    missing_count: int
    null_count: int
    json_type_counts: JsonTypeCounts
    finite_numeric_min: int | float | None
    finite_numeric_max: int | float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "field_name": self.field_name,
            "present_count": self.present_count,
            "missing_count": self.missing_count,
            "null_count": self.null_count,
            "json_type_counts": self.json_type_counts.to_dict(),
            "finite_numeric_min": self.finite_numeric_min,
            "finite_numeric_max": self.finite_numeric_max,
        }


@dataclass(frozen=True)
class FloodProfileResult:
    profile_schema_version: str
    page_count: int
    feature_count: int
    configured_key_check_complete: bool
    issue_categories: tuple[str, ...]
    geometry_member_counts: MemberCounts
    geometry_type_counts: tuple[GeometryTypeCount, ...]
    properties_member_counts: MemberCounts
    property_fields: tuple[PropertyFieldProfile, ...]
    observation_scope: str = "structural_only"

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_schema_version": self.profile_schema_version,
            "page_count": self.page_count,
            "feature_count": self.feature_count,
            "configured_key_check_complete": self.configured_key_check_complete,
            "issue_categories": list(self.issue_categories),
            "geometry_member_counts": self.geometry_member_counts.to_dict(),
            "geometry_type_counts": [
                item.to_dict() for item in self.geometry_type_counts
            ],
            "properties_member_counts": self.properties_member_counts.to_dict(),
            "property_fields": [item.to_dict() for item in self.property_fields],
            "observation_scope": self.observation_scope,
        }


@dataclass
class _FieldAccumulator:
    present: int = 0
    null: int = 0
    types: dict[str, int] = field(default_factory=dict)
    numeric_min: int | float | None = None
    numeric_max: int | float | None = None


def profile_validated_pages(
    pages: Iterable[ValidatedProfilePage],
) -> FloodProfileResult:
    """Aggregate safe structural observations from sealed Phase 2B page pairs."""

    try:
        sequence = tuple(pages)
    except Exception:
        raise FloodProfileError("invalid_profile_input") from None
    if not sequence or not all(isinstance(page, ValidatedProfilePage) for page in sequence):
        raise FloodProfileError("invalid_profile_input")

    validations = tuple(page.validation for page in sequence)
    try:
        run = validate_page_sequence(validations)
    except SourceValidationError:
        raise FloodProfileError("validation_failed") from None

    geometry_members = {name: 0 for name in ("missing", "null", "object", "other")}
    properties_members = {name: 0 for name in ("missing", "null", "object", "other")}
    geometry_types: dict[str, int] = {}
    property_fields: dict[str, _FieldAccumulator] = {}
    feature_count = 0

    for page in sequence:
        try:
            features = _validated_features(page.parsed, page.validation)
        except SourceValidationError:
            raise FloodProfileError("invalid_validated_page") from None
        for feature in features:
            feature_count += 1
            _count_geometry(feature, geometry_members, geometry_types)
            _count_properties(feature, properties_members, property_fields)

    geometry_member_result = MemberCounts(**geometry_members)
    properties_member_result = MemberCounts(**properties_members)
    geometry_type_result = tuple(
        GeometryTypeCount(name, geometry_types[name])
        for name in sorted(geometry_types)
    )
    property_result = tuple(
        _finalize_field(name, accumulator, feature_count)
        for name, accumulator in sorted(property_fields.items())
    )

    if (
        feature_count != run.total_features
        or geometry_member_result.total != feature_count
        or properties_member_result.total != feature_count
        or sum(item.count for item in geometry_type_result)
        != geometry_member_result.object
        or any(
            field.present_count + field.missing_count != feature_count
            or field.json_type_counts.total != field.present_count
            or field.null_count != field.json_type_counts.null
            for field in property_result
        )
    ):
        raise FloodProfileError("profile_invariant_failed")

    return FloodProfileResult(
        profile_schema_version=PROFILE_SCHEMA_VERSION,
        page_count=run.page_count,
        feature_count=feature_count,
        configured_key_check_complete=run.configured_key_check_complete,
        issue_categories=run.issue_categories,
        geometry_member_counts=geometry_member_result,
        geometry_type_counts=geometry_type_result,
        properties_member_counts=properties_member_result,
        property_fields=property_result,
    )


def _count_geometry(
    feature: dict[str, Any],
    members: dict[str, int],
    types: dict[str, int],
) -> None:
    geometry = feature.get("geometry", _MISSING)
    if geometry is _MISSING:
        members["missing"] += 1
    elif geometry is None:
        members["null"] += 1
    elif isinstance(geometry, dict):
        members["object"] += 1
        observed = geometry.get("type")
        label = (
            observed
            if isinstance(observed, str) and observed in _GEOMETRY_TYPES
            else "unknown"
        )
        types[label] = types.get(label, 0) + 1
    else:
        members["other"] += 1


def _count_properties(
    feature: dict[str, Any],
    members: dict[str, int],
    fields: dict[str, _FieldAccumulator],
) -> None:
    properties = feature.get("properties", _MISSING)
    if properties is _MISSING:
        members["missing"] += 1
        return
    if properties is None:
        members["null"] += 1
        return
    if not isinstance(properties, dict):
        members["other"] += 1
        return
    members["object"] += 1
    for name, value in properties.items():
        accumulator = fields.setdefault(name, _FieldAccumulator())
        accumulator.present += 1
        value_type = _json_type(value)
        accumulator.types[value_type] = accumulator.types.get(value_type, 0) + 1
        if value is None:
            accumulator.null += 1
        elif type(value) in {int, float}:
            if type(value) is float and not math.isfinite(value):
                raise FloodProfileError("nonfinite_number")
            if accumulator.numeric_min is None or value < accumulator.numeric_min:
                accumulator.numeric_min = value
            if accumulator.numeric_max is None or value > accumulator.numeric_max:
                accumulator.numeric_max = value


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) is int:
        return "integer"
    if type(value) is float:
        if not math.isfinite(value):
            raise FloodProfileError("nonfinite_number")
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise FloodProfileError("invalid_validated_page")


def _finalize_field(
    name: str,
    accumulator: _FieldAccumulator,
    feature_count: int,
) -> PropertyFieldProfile:
    counts = JsonTypeCounts(**{
        field_name: accumulator.types.get(field_name, 0)
        for field_name in JsonTypeCounts.__dataclass_fields__
    })
    return PropertyFieldProfile(
        field_name=name,
        present_count=accumulator.present,
        missing_count=feature_count - accumulator.present,
        null_count=accumulator.null,
        json_type_counts=counts,
        finite_numeric_min=accumulator.numeric_min,
        finite_numeric_max=accumulator.numeric_max,
    )
