# Phase 3A Handoff — Pattani Infrastructure Sources

Verified: **2026-09-23 UTC**. This is a compact handoff; consult the linked official sources before implementation.

Repository status: this file was prepared as an **uncommitted review artifact** after the original read-only Phase 3A research. Check the current repository state separately before review or approval.

## Project checkpoint

- Phase 1 Pattani flood-frequency ingestion is complete and immutable.
- Phase 2 validation, profiling, and neutral JSONL transformation is complete.
- Phase 3 begins with infrastructure source selection and contract verification.
- The original Phase 3A research made no dataset/API request, download, login, or repository change; this handoff was created afterward as an uncommitted review artifact.

## Decisions

### Roads — preferred

**Department of Rural Roads: Web Service Rural Road Network**

- Official government source with JSON and bulk Shapefile resources.
- Documents route code/name, location, physical details, slope details, and coordinates.
- Catalog says annual updates, public/open access, `Open Data Common`, and scale `1:4,000`.
- The catalog displays a Shapefile resource labeled for fiscal year 2570; this does not establish that all dataset contents are current.
- Dataset metadata still records 1 October 2020 as the latest data/reference date, while the catalog/resource listing was modified later.
- Source: [MOT catalog](https://datagov.mot.go.th/th/dataset/dataset_41_01)
- Resource metadata: [Shapefile](https://datagov.mot.go.th/th/dataset/dataset_41_01/resource/4b670506-05e3-4cb2-80d7-06fc0745d21d)

**Blockers:** CRS, exact geometry type, Pattani completeness/filter, positional accuracy, full license terms, and web-service limits are not documented. Coverage is rural roads only; it is not a verified complete road network.

**Fallback:** OpenStreetMap, only if broader coverage is required. It is non-government, community-maintained, and ODbL-licensed. Roads are generally tagged ways using WGS84 nodes. ODbL attribution/share-alike obligations need a separate design. Sources: [copyright/license](https://www.openstreetmap.org/copyright), [data model](https://wiki.openstreetmap.org/wiki/OSM_id), [downloads](https://wiki.openstreetmap.org/wiki/Extract).

### Healthcare — preferred

**DGA: Government Hospital Coordinates from CITIZENinfo**

- Public national government-facility coordinate dataset.
- Bulk CSV, XLSX, and ZIP; no authentication documented.
- Published under CC BY 4.0.
- Static snapshot: published 2020; metadata maintained 2021; no ongoing update documented.
- Source: [data.go.th catalog](https://data.go.th/th/dataset/health-citizeninfo?is_fullscreen=1)
- CSV resource ID: `2d45b0c6-75e9-4463-888d-ef364ad164fb`; [catalog-provided direct-download URL](https://data.go.th/dataset/00170665-bda1-4f4a-ad7c-52dac7abc7a5/resource/2d45b0c6-75e9-4463-888d-ef364ad164fb/download/citizeninfo_health_20200314.csv), not requested during verification.
- ZIP resource ID: `c12a9e8d-8fa6-49a8-80d8-596e66329088`; [catalog-provided direct-download URL](https://data.go.th/dataset/00170665-bda1-4f4a-ad7c-52dac7abc7a5/resource/c12a9e8d-8fa6-49a8-80d8-596e66329088/download/citizeninfo_health_20200314.zip), not requested during verification.
- License: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

Metadata verification used four official catalog/metadata request attempts on `data.go.th`: three attempts to `/th/dataset/health-citizeninfo` (two successful catalog reads and one local-client failure) and one unsuccessful access attempt to `/api/3/action/package_show?id=health-citizeninfo`. No resource URL was requested and no dataset file was downloaded.

**Blockers:** CRS, complete field dictionary, stable identifier, Pattani completeness/filter, collection date, positional accuracy, and coverage of non-government facilities are unresolved.

**Fallback:** MOPH HCODE API for current registry identifiers and province filtering.

- Endpoint: `GET https://hcode.moph.go.th/api/health_office/`
- JSON pagination; `province` and `province_code` filters are documented.
- JWT Bearer authentication requires registration.
- Limits: 60 requests/minute and 1,000/day; page size up to 1,000 for general accounts and 10,000 for government accounts.
- Sources: [API guide](https://hcode.moph.go.th/api_documentation/), [parameters](https://hcode.moph.go.th/api_documentation/endpoint_and_queryparameter/), [permissions/rate limits](https://hcode.moph.go.th/api_documentation/data_and_permission/)

**Blockers:** reuse license, update cadence, CRS, and coordinate-field availability by account are unresolved. Do not request credentials or call the API yet.

## Phase 3B gate

Phase 3A contract verification is complete for the selected catalog resources. Phase 3B still requires separate download authorization. Before any download:

1. Reverify the exact DRR Shapefile and DGA resource metadata and license evidence.
2. Resolve or formally retain CRS as unknown; never infer EPSG/SRID.
3. Approve exact URLs, at most two downloads, byte limits, destination paths, immutable naming, metadata capture, and SHA-256 verification.
4. After download, inspect schemas and Pattani coverage offline under separate authorization.
5. Do not automatically use OSM or HCODE if a preferred source fails.

## Security and reporting

- Never expose credentials, tokens, individual facilities, coordinates, or response bodies.
- No `.env` access is needed for source-contract work or public bulk downloads.
- Keep downloaded inputs immutable and Git-ignored with retrieval metadata and hashes.
- Do not infer CRS, field meanings, completeness, or positional accuracy.

## Recommended next action

Review and approve the completed Phase 3A contract evidence for the exact DRR Shapefile and DGA bulk resources. Phase 3B begins only after separate download authorization.
