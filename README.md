# Thai Flood Disruption Intelligence - Pattani

Phase one focuses exclusively on ingesting GISTDA historical flood data for Pattani, Thailand.

## Scope

This phase will provide the foundation for a reliable flood-data pipeline:

- Download GISTDA historical flood responses using only verified request behavior.
- Preserve credential-sanitized source artifacts immutably in `data/raw/`; existing artifacts must never be overwritten.
- Validate officially documented behavior separately from structure observed in prior API tests.
- Derive processed data reproducibly from identified sanitized source artifacts in `data/processed/`.
- Add focused tests for ingestion, validation, and transformation.

Machine learning, dashboards, databases, population, roads, hospitals, schools, and derived features are out of scope for this phase.

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
