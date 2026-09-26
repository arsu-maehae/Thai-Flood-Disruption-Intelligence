# Verified Offline Pattani Exposure Report

Phase 4C completed offline on **2026-09-27 UTC**. Report
`exploratory-exposure-report-v1-20260927-01` presents only the verified,
aggregate outputs from Phase 4A and Phase 4B. It does not recompute spatial
intersections or expose record-level data.

## Report contents

The self-contained `index.html` uses semantic HTML, inline CSS, and inline SVG.
It needs no server, internet connection, external font, analytics, map tile, or
third-party JavaScript. Accessible tables accompany the two separately scaled
annual charts.

Headline aggregates are:

- 32,358 road segments: 4,919 ever exposed and 27,439 non-exposed.
- 138 healthcare candidates: 18 ever exposed and 120 non-exposed.
- `freq` equaled the sum of the fourteen binary yearly fields for all 112,073
  validated flood features; mismatch and missing/invalid counts were zero.

Annual road exposure counts for 2011 through 2024 are 74, 15, 1,009, 232, 15,
42, 4,042, 0, 121, 548, 698, 648, 629, and 840. Corresponding healthcare counts
are 0, 0, 2, 1, 0, 0, 15, 0, 0, 3, 3, 4, 3, and 6. Annual counts must not be
summed and presented as unique infrastructure totals.

The largest observed ever-exposed road-category counts are `service` 1,601,
`residential` 1,566, `unclassified` 476, `track` 395, and `tertiary` 310.
These labels do not establish drivability, access, importance, or condition.

## Immutable outputs

Stored beneath
`data/processed/reports/pattani/exploratory-exposure-report-v1-20260927-01/`:

| Output | Bytes | SHA-256 |
| --- | ---: | --- |
| `index.html` | 10,114 | `ad4cea9cdfc95f291a9ba9ae674355930f6f68a3779f61f2dcdc8a9f797e3b2d` |
| `report_summary.json` | 2,257 | `7acbb3c6b0ce732dc5b2e38e9b79a78fe00f508319e0f5455edc2e8f6b217a67` |
| `report_manifest.json` | 1,297 | `94cc03447e5c88fd2edc8f0a737a20d6c7419f95cdf6bf97df1fc831714ff150` |

The manifest was published last. Read-only verification reconciled the report
files, input manifests, hashes, byte counts, years, category totals, and report
scope and returned complete with zero issues.

## Interpretation boundary

“Exposed” means exact geometric intersection under the reviewed exploratory
coordinate policy. This report is not a disruption, risk, severity,
accessibility, prediction, emergency-response, or completeness product. No map
is included because official GISTDA and DGA CRS remain unresolved. Provider
field meanings, temporal semantics, completeness, positional accuracy, and
snapshot consistency also remain unresolved.
