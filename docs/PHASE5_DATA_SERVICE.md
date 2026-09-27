# Phase 5 Local Aggregate Data Service

Phase 5 adds a local PostGIS-backed store and read-only API for the verified
Phase 4C aggregates. Its scope is **exploratory geometric exposure
aggregates**. It stores no raw identifiers, coordinates, geometry, source
properties, credentials, request headers, provider responses, or full provider
links.

Real PostGIS integration was not run during implementation because the Docker
client and Compose were installed but the daemon was unavailable. The
implementation and synthetic tests completed without substituting SQLite.

## Architecture and data flow

```text
verified Phase 4A/4B outputs
            │
            ▼
verified Phase 4C report + manifest
            │ read-only verification
            ▼
transactional immutable loader
            │
            ▼
loopback PostGIS aggregate tables
            │ parameterized SELECT only
            ▼
local read-only FastAPI
```

PostGIS is enabled as a future database foundation, but Phase 5 creates no
geometry column and performs no spatial operation. Official GISTDA and DGA CRS
remain unresolved.

## Configuration

The committed Compose image is `postgis/postgis:16-3.5`. PostgreSQL is exposed
only on `127.0.0.1` and uses the named volume
`thai-flood-phase5-postgis-data`. Required variables are:

- `PHASE5_DB_HOST=127.0.0.1`
- `PHASE5_DB_PORT=55432`
- `PHASE5_DB_NAME=pattani_exposure`
- `PHASE5_DB_USER=pattani_app`
- `PHASE5_DB_PASSWORD=<new strong local password>`

When Docker is available, generate a new ignored `.env.phase5.local` without
printing its password:

```powershell
$bytes = New-Object byte[] 32
[Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
$password = [Convert]::ToBase64String($bytes)
@"
PHASE5_DB_HOST=127.0.0.1
PHASE5_DB_PORT=55432
PHASE5_DB_NAME=pattani_exposure
PHASE5_DB_USER=pattani_app
PHASE5_DB_PASSWORD=$password
"@ | Set-Content -LiteralPath .env.phase5.local -Encoding utf8
Remove-Variable password, bytes
```

Never reuse GISTDA credentials and never pass a database password on a command
line.

## Start, load, and serve

Start only the project service and wait for its health check:

```powershell
docker compose --env-file .env.phase5.local up -d postgis
docker compose --env-file .env.phase5.local ps
```

Load the local variables into the current PowerShell process without displaying
them, safely apply schema version `1.0`, and load the verified report:

```powershell
Get-Content -LiteralPath .env.phase5.local | ForEach-Object {
  if ($_ -match '^([^#=]+)=(.*)$') {
    [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
  }
}
python -m src.database.exposure_store `
  --processed-root data/processed `
  --report-id exploratory-exposure-report-v1-20260927-01 `
  --apply-schema
```

The schema installer creates the project namespace only when it is empty. It
refuses missing, extra, or wrong-version project tables. The loader reruns the
read-only report verifier, loads one transaction, re-queries every aggregate,
and rolls back partial failure. Reloading identical immutable content is safe;
the same report ID with different content is rejected.

Start the API on loopback only:

```powershell
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

Local interactive documentation is available at `/docs`. Stop and restart
without deleting the persistent volume:

```powershell
docker compose --env-file .env.phase5.local stop
docker compose --env-file .env.phase5.local start postgis
```

Do not use `docker compose down -v`, `docker volume rm`, or Docker prune
commands for this project.

## Relational contract

Schema `pattani_exposure`, version `1.0`, contains:

- `report_versions`: immutable provenance, policy, CRS status, manifest hash,
  interpretation scope, and caveats.
- `exposure_headline`: road and healthcare total/exposed/non-exposed counts.
- `annual_exposure`: ordered 2011–2024 exposed counts by infrastructure type.
- `road_category_exposure`: deterministic observed-category aggregates.
- `frequency_consistency`: structural match/mismatch/missing counts.

Primary keys, foreign keys, range constraints, count reconciliation, and
database triggers protect immutable versions. Application SQL is parameterized.

## Read-only API contract

- `GET /health`
- `GET /v1/metadata`
- `GET /v1/exposure/summary`
- `GET /v1/exposure/annual`
- `GET /v1/exposure/road-categories`

The four versioned endpoints accept only an optional strictly validated
`report_id`; omission selects the latest successfully loaded immutable report.
Annual results sort by year and infrastructure type. Road categories sort by
their observed label. There are no application POST, PUT, PATCH, or DELETE
routes, and permissive CORS is not installed.

## Safe failure categories

- `input_invalid` or `input_verification_failed`: the report did not satisfy
  the committed aggregate contract.
- `schema_drift`: the project database namespace is not the expected version.
- `version_collision`: an immutable report ID exists with different content.
- `reconciliation_failed`: inserted data did not re-query identically.
- `database_unavailable` or `database_failure`: fixed safe database outcomes;
  connection strings and arbitrary database messages are not returned.

## Scientific and operational limits

Geometric intersection is not confirmed disruption, risk, severity,
accessibility, prediction, damage, or complete coverage. Official GISTDA and
DGA CRS, field semantics, positional accuracy, snapshot consistency, and
provider completeness remain unresolved. The service must remain local until a
separate security and deployment review.

The earlier verified backup covers the Phase 1 Pattani flood-source subtree.
The later infrastructure and Phase 2–4 generated data are **not confirmed
backed up** because the subsequent MVP backup prompt was cancelled. GitHub does
not back up ignored generated data or the PostGIS volume.
