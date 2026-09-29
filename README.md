# Thai Flood Disruption Intelligence — Pattani

An end-to-end geospatial data science case study asking: **where do observed
historical flood geometries intersect Pattani roads and healthcare location
candidates?** The result is an exploratory, reproducible analysis—not a claim
of confirmed disruption, damage, accessibility, prediction, risk, or complete
coverage.

![Pattani exposure headline](docs/assets/portfolio/headline_exposure.png)

## Portfolio snapshot

- **Scale:** 112,073 historical flood features, 32,358 road segments, and 138
  healthcare address-text candidates.
- **Method:** credential-safe immutable ingestion, structural validation,
  deterministic transformation, exact geometric intersections, temporal
  aggregation, PostGIS publication, and an accessible local dashboard.
- **Finding 1:** 4,919 road segments had an observed intersection—**15.2%** of
  the reviewed road-segment snapshot.
- **Finding 2:** 18 healthcare candidates had an observed intersection—**13.0%**
  of the candidate records.
- **Finding 3:** 2017 had the maximum observed annual counts: **4,042 road
  segments** and **15 healthcare candidates**.

Read the [portfolio case study](docs/PORTFOLIO_CASE_STUDY.md) or open the
[executed analysis notebook](notebooks/pattani_flood_exposure_eda.ipynb).
The interactive local dashboard is served at `http://127.0.0.1:8000/dashboard/`
using the commands in [Phase 7 Spatial Map](docs/PHASE7_SPATIAL_MAP.md).

## Technology

Python · requests · PyOsmium · Shapely · Matplotlib · PostgreSQL/PostGIS ·
FastAPI · Docker Compose · browser-native HTML/CSS/JavaScript/Canvas · pytest

## Run locally

```text
python -m venv venv
python -m pip install -r requirements.txt
pytest -q
```

The notebook and static portfolio charts use verified repository-relative
aggregates and require no live API or database. The dashboard requires the
preserved local PostGIS volume; follow `docs/PHASE7_SPATIAL_MAP.md` and bind
services only to loopback.

## Interpretation limits

Road records are clipped segments, not unique roads. Healthcare records are
address-text candidates, not verified facilities. Official GISTDA and DGA CRS,
field meanings, positional accuracy, completeness, ordering, and snapshot
consistency remain unresolved. Annual counts may overlap and must not be summed
as unique infrastructure totals. The map’s non-exposed road context is a
non-representative deterministic subset, so visual proportions are not
prevalence.

## Project evolution

The repository progressed from a secure Phase 1 flood-ingestion foundation to
validated infrastructure integration, exploratory spatial and temporal
analysis, immutable aggregate publication, and a local analytical dashboard.

## Project Structure

```text
src/
  ingestion/       GISTDA access and sanitized source persistence
  validation/      Input structure and quality checks
  transformation/  Conversion to the processed project format
  features/        Reserved for a later phase
data/
  raw/             Immutable, credential-sanitized source-layer artifacts
  processed/       Validated and transformed flood data
  features/        Reserved for a later phase
tests/             Unit and pipeline tests
```

## API Contract

API information is maintained in three explicit categories:

- **Officially documented:** behavior supported by GISTDA official documentation.
- **Previously observed:** behavior seen in earlier API tests but not necessarily specified by official documentation.
- **Not yet verified:** behavior that must not be assumed or implemented as an API contract.

The pipeline will use only officially documented behavior or explicitly scoped observed behavior that has been reviewed and approved. It will not infer response-field meanings, geometry semantics, coordinate reference systems, pagination behavior, or a final processed-data schema.

Live API requests require explicit user approval. Any API key previously exposed must be treated as compromised and replaced with a rotated key before a live request is made.

Configuration values belong in a local `.env` file. Start from `.env.example` and never commit credentials.

## Source Artifact Security and Provenance

GISTDA response links were previously observed echoing an active credential in an `api_key` query parameter. This is observed behavior, not an official response contract, and an active credential must never be persisted.

For this source, the original response exists only in memory. The ingestion workflow records its SHA-256 and byte count, removes credential-named fields and link query parameters, and deterministically serializes a `.sanitized.json` source artifact. The stored artifact has a separate SHA-256 and byte count, and the original response body is not persisted. Files under `data/raw/` for this source are therefore sanitized source-layer artifacts, not untouched provider responses. Stored artifacts remain immutable and retain explicit lineage to their retrieval metadata.

## Development

Create an environment and install dependencies:

```text
python -m venv .venv
python -m pip install -r requirements.txt
```

Run tests with:

```text
pytest
```

## Ingestion operator

The guarded operator provides network-free `preflight` and `verify` commands and a separately authorized `run` command. Phase 1 supports only this repository's `data/raw` output root. A live run requires explicit user authorization as well as both runtime safeguards; successful preflight alone is not authorization.

See [Pattani Ingestion Operations](docs/INGESTION_OPERATIONS.md) for parameter selection, commands, failure handling, verification, and current limitations. Do not begin full ingestion without separate approval.

## Verified offline exposure report

Phase 4C publishes a deterministic, self-contained exploratory report at
`data/processed/reports/pattani/exploratory-exposure-report-v1-20260927-01/index.html`.
It works locally without a server or internet connection and contains only
verified Phase 4A/4B aggregates. It intentionally contains no geographic map
because official GISTDA and DGA CRS remain unresolved.

The report is not a disruption, risk, severity, accessibility, prediction,
emergency-response, or completeness product. See
[Verified Offline Pattani Exposure Report](docs/EXPLORATORY_EXPOSURE_REPORT.md)
for provenance, hashes, aggregate results, and interpretation limits.

## Local aggregate data service

Phase 5 provides a versioned PostGIS schema and read-only FastAPI for the
verified Phase 4 aggregates. It stores aggregates and provenance only—no raw
identifiers, coordinates, geometry, properties, credentials, or response
bodies. The API is intended for loopback use and describes its data as
**exploratory geometric exposure aggregates**, not disruption or risk.

See [Phase 5 Local Aggregate Data Service](docs/PHASE5_DATA_SERVICE.md) for the
database tables, configuration, loader, endpoints, safe restart/stop commands,
and scientific limitations.

## Local interactive dashboard

Phase 6 provides a responsive, accessible, same-origin dashboard at
`/dashboard/` using the local read-only aggregate service. It uses browser-native
HTML, CSS, and JavaScript with no map, external assets, analytics, cookies, or
trackers. See [Phase 6 Local Interactive Exposure Dashboard](docs/PHASE6_DASHBOARD.md)
for start/stop commands, security controls, interactions, and interpretation
limits.

Phase 7 extends the same local dashboard with a bounded browser-native Canvas
map sourced only from verified generated outputs. It sends all exposed road
segments, a bounded deterministic non-representative subset of non-exposed road
context, and all healthcare candidates; visual proportions are not prevalence;
it sends no flood polygons, source identifiers, external tiles, or provider
properties. See [Phase 7 Local Exploratory Spatial Map](docs/PHASE7_SPATIAL_MAP.md)
for the route, limits, controls, commands, and non-authoritative interpretation.
