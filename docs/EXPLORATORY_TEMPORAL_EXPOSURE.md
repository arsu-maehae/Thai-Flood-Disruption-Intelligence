# Exploratory Pattani Temporal Exposure

Phase 4B completed offline on **2026-09-27 UTC** under policy
`exploratory_non_authoritative`. It treats `y_2011` through `y_2024` and `freq`
as structural fields only. No flood meaning, severity, disruption, risk,
accessibility, completeness, or official CRS is inferred.

## Structural observation

Across all 112,073 validated flood features, `freq` equaled the arithmetic sum
of the fourteen binary yearly fields in 112,073 cases. Mismatch and
missing/invalid counts were both zero. This is an observed structural
relationship in this snapshot, not an official field-semantic contract.

| Year | Active flood features | Exposed roads | Exposed healthcare candidates |
| --- | ---: | ---: | ---: |
| 2011 | 4,372 | 74 | 0 |
| 2012 | 1,706 | 15 | 0 |
| 2013 | 37,291 | 1,009 | 2 |
| 2014 | 18,971 | 232 | 1 |
| 2015 | 1,705 | 15 | 0 |
| 2016 | 6,441 | 42 | 0 |
| 2017 | 88,561 | 4,042 | 15 |
| 2018 | 0 | 0 | 0 |
| 2019 | 15,453 | 121 | 0 |
| 2020 | 30,773 | 548 | 3 |
| 2021 | 39,273 | 698 | 3 |
| 2022 | 37,432 | 648 | 4 |
| 2023 | 44,587 | 629 | 3 |
| 2024 | 47,723 | 840 | 6 |

Each infrastructure record is counted at most once per year, regardless of
overlapping flood polygons. The union across years reconciled exactly with
Phase 4A: 4,919 road segments and 18 healthcare candidates were ever exposed.
Observed road-category aggregation is descriptive only; the largest
ever-exposed segment counts were `service` 1,601, `residential` 1,566,
`unclassified` 476, `track` 395, and `tertiary` 310. These labels are not
interpreted as drivability or importance.

## Immutable outputs

Stored beneath
`data/processed/analysis/pattani/temporal-exposure-v1-20260926-01/`:

| Output | Bytes | SHA-256 |
| --- | ---: | --- |
| `annual_exposure_summary.json` | 876 | `0534568e381624c800e71960d4c5946a8cea9423acea66b54370493321bfba9f` |
| `road_category_annual_exposure.csv` | 1,054 | `2bb17d8677fa275362e65347f51e292c57a6fa7f90af451cfcd7f4b6d81c50f6` |
| `healthcare_annual_exposure.csv` | 191 | `983ed3fdb0b7c0d48f510f798c2a09c94c33e6ca393009d2e4ca459f24d75938` |
| `frequency_consistency.json` | 393 | `030badc68ee5b8c9a86684d5608a6390540835102cc55449c7ee5b8e8d520ef0` |
| `analysis_manifest.json` | 2,937 | `afcbec289f108c39c295db200cb7beeb3453b1fd38b5d7059b91c8fd87fa0891` |

The read-only verifier reported complete with zero issues. Processing used one
page-bounded Shapely spatial index per verified flood page, exact intersection,
and annual bitmask union. Source and prior derived datasets were not modified.

## Remaining boundary

The exploratory coordinate policy from Phase 4A remains unchanged. Provider
field semantics, temporal interpretation, survey cadence, completeness,
positional accuracy, and official GISTDA/DGA CRS remain unresolved. Any Phase
4C scoring, prioritization, or operational decision product requires separate
evidence, design review, and authorization.
