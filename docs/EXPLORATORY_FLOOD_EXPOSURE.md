# Exploratory Pattani Flood Exposure

Phase 4A completed offline on **2026-09-26 UTC** under policy
`exploratory_non_authoritative`. Here, “exposed” means only that a derived road
segment or healthcare candidate point geometrically intersects at least one
validated flood `MultiPolygon`. It is not a disruption, severity, accessibility,
risk, completeness, or service-impact score.

## Coordinate policy

- OSM coordinates use the officially documented OSM coordinate model recorded
  in `docs/SPATIAL_REFERENCE_CONTRACT.md`.
- For this exploratory run only, GISTDA GeoJSON geometry was interpreted using
  RFC 7946 longitude/latitude ordering. This is a standards-based project
  interpretation, not an explicit GISTDA CRS statement.
- All 138 DGA normal-axis coordinate candidates intersected the validated
  Pattani boundary while zero swapped-axis candidates did. This unique
  observational consistency permitted exploratory point use; it does not
  establish an official DGA CRS.
- No reprojection, repair, buffer, simplification, metric distance, or metric
  area calculation occurred.

## Aggregate result

- Flood geometries: 112,073 validated `MultiPolygon` features across 113 pages.
- Roads: 32,358 segments; 4,919 exposed and 27,439 non-exposed.
- Healthcare: 138 address-text candidates; 18 exposed and 120 non-exposed.
- Rejected or invalid inputs: zero.

Outputs are stored beneath
`data/processed/analysis/pattani/exploratory-exposure-v1-20260925-01/`:

| Output | Bytes | SHA-256 |
| --- | ---: | --- |
| `road_exposure.jsonl` | 3,467,840 | `779581d5a3c30199c85adb16876937d2e739deef8c2b46fdf99127f45de77b94` |
| `healthcare_exposure.jsonl` | 14,114 | `b1f33a357c81acb96e87a1f1078a3210737fb052c9bca9630e307be24da87803` |
| `exposure_summary.json` | 954 | `33de3fb293ce83f5f3fab05f064ff4521e1ecbfbdbd126a3d62d2f2333ba0f76` |
| `analysis_manifest.json` | 2,452 | `b40617adef7e3663a1d0ce1042c90c2f36d86575406500f94f551348e7012a1f` |

The implementation verified every input hash, byte count, sequence, geometry,
and lineage reference. It used page-bounded Shapely `STRtree` indexes (at most
1,000 flood features per index) followed by exact `intersects` evaluation. The
read-only output verifier reported zero issues.

## Limitations and Phase 4B gate

Official GISTDA and DGA CRS statements remain unresolved. Temporal alignment,
provider completeness, road access, facility status, positional accuracy, and
all source-field semantics remain unverified. Phase 4B must separately decide
whether to obtain stronger CRS/semantic evidence or retain the analysis as
exploratory; no disruption or risk scoring should begin implicitly.
