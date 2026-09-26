# Data Sources

## Phase 1: Flood

Phase 1 is limited to historical flood recurrence data for Pattani Province.

### Officially documented

Verified against the official GISTDA Open API documentation on **2026-08-28 UTC**.

| Item | Verified information |
|---|---|
| Provider | GISTDA (Geo-Informatics and Space Technology Development Agency) |
| Dataset | ข้อมูลน้ำท่วมซ้ำซาก / Historical Flood Recurrence |
| OpenAPI version | `3.0.3` |
| API documentation version | `1.0` |
| Base URL | `https://api-gateway.gistda.or.th/api/2.0/resources` |
| Endpoint | `GET /features/flood-freq` |
| Full request URL | `https://api-gateway.gistda.or.th/api/2.0/resources/features/flood-freq` |
| Official API documentation | [GISTDA Disaster Open API](https://disaster.gistda.or.th/services/open-api) |
| OpenAPI definition inspection | Inspected through the official GISTDA documentation application; no stable official OpenAPI JSON or YAML URL was identified |

The official documentation describes this endpoint as a summary of recurring flood-area data.

#### Authentication and required headers

| Item | Verified information |
|---|---|
| Authentication scheme | API key |
| Required authentication header | `API-Key` |
| Authentication value format or prefix | **Not yet verified** |
| Invalid API key response | HTTP `401`, `application/json`; description: API key is invalid |
| Other required headers | **Not yet verified** |

The documentation defines the OpenAPI security scheme as `type: apiKey`, with the key sent in the request header named `API-Key`. It does not document the key format or any prefix.

#### Query parameters

All parameters documented for this endpoint are optional.

| Parameter | Location | Type | Required | Documented constraints/default | Documented purpose |
|---|---|---|---|---|---|
| `bbox` | Query | String | No | Described as 4 or 6 numbers: `x-min,y-min,[z-min],x-max,y-max,[z-max]` | Filter results to a bounding area |
| `limit` | Query | Integer | No | Minimum `1`; maximum `10000`; default `1` | Limit the number of results |
| `offset` | Query | Integer | No | Default `0`; description says values start at `0` | Skip a number of results |
| `pv_idn` | Query | String | No | No default documented | Filter by province ID |
| `ap_idn` | Query | String | No | No default documented | Filter by district ID |
| `tb_idn` | Query | String | No | No default documented | Filter by subdistrict ID |

The OpenAPI schema declares `bbox` as a string while also describing its contents as 4 or 6 numbers. This document preserves that definition without inferring a different request type.

#### Pagination

The endpoint officially documents `limit` and `offset` as result-window controls. Total-count fields, next/previous links, stable ordering, and termination or end-of-results behavior are **Not yet verified**.

#### Responses

| HTTP status | Media type | Documented meaning | Body schema |
|---|---|---|---|
| `200` | `application/json` | Request succeeded | **Not yet verified** |
| `400` | `application/json` | Error processing the request | **Not yet verified** |
| `401` | `application/json` | Invalid API key | **Not yet verified** |
| `404` | `application/json` | No data found | **Not yet verified** |

The documentation declares JSON media types but provides no successful-response or error-response body schema for this endpoint.

#### Spatial data and operational behavior

| Item | Verification status |
|---|---|
| Response structure and field meanings | **Not yet verified** |
| Feature properties and their business meanings | **Not yet verified** |
| Geometry representation or geometry type | **Not yet verified** |
| Coordinate reference system (CRS) | **Not yet verified** |
| Empty successful response behavior | **Not yet verified** |
| Body structure for `400`, `401`, or `404` | **Not yet verified** |
| Pagination ordering and termination behavior | **Not yet verified** |
| Rate limits and retry guidance | **Not yet verified** |

### Previously observed

| Item | Observed information |
|---|---|
| Geographic scope requested | Pattani Province |
| Request value used | `pv_idn=94` |
| Exploratory request | HTTP `200`; content type `application/geo+json`; 10 features returned |
| Response structure | GeoJSON `FeatureCollection` was observed |
| Returned counts | `numberReturned=10`; `numberMatched=112073` |
| Geometry type | `MultiPolygon` was observed |
| Link relations | `self`, `alternate`, and `next` were observed |
| Credential echo | Successful response `links[].href` values contained an `api_key` query parameter |
| Exploratory artifact disposition | The first 10-record artifact was deleted after the echoed credential was discovered, and the credential was rotated again; the deleted sample is not a retained source artifact |

These observations establish only what was seen in the earlier test. They do not establish an officially documented response or link contract, geometry contract, CRS, field meaning, pagination rule, count semantic, or official mapping of `pv_idn=94` to Pattani.

#### Milestone 7 controlled pagination probe

Verified on **2026-08-29 UTC** as **Previously observed** behavior:

- Three controlled requests used `pv_idn=94`, `limit=10`, and offsets `0`, `10`, and `112070`; returned feature counts were `10`, `10`, and `3`.
- `numberMatched=112073` was present and consistent across all three responses.
- All observed feature IDs were non-empty strings and unique within each page. No ID overlap was observed between offsets `0` and `10`.
- The current offset-0 feature-ID order matched the earlier safe sample, but this does not establish stable ordering.
- Observed next offsets were `10` and `20`. The tail response was partial, had no `next` relation, and satisfied `112070 + 3 = 112073`.
- Link relations observed across the probe included `self`, `alternate`, `next`, and `prev`.
- All persisted artifacts were credential-sanitized and passed integrity verification.

These findings are observations, not official pagination guarantees. Stable ordering, snapshot consistency, count semantics, and termination behavior remain **Not yet verified** as a general contract. Response links must never be followed as trusted request instructions.

#### Bounded page-size probe

Observed on **2026-09-19 UTC** as **Previously observed** behavior:

- Exactly one request used `pv_idn=94`, `limit=1000`, and offset `0`; no retry occurred, and response links were not followed.
- The response was HTTP `200` with content type `application/geo+json`.
- `numberReturned=1000` and `numberMatched=112073` were observed.
- The original response was `2836590` bytes. The stored credential-sanitized artifact was `6544748` bytes.
- Lifecycle time to the terminal record was `1.783328` seconds.
- Because the bounded probe used `max_pages=1` and received a full page, the implementation published a valid failed terminal with category `max_pages_exhausted`.
- Run counts were attempted `1`, persisted `1`, validated `1`, and journaled `1`.
- Offline verification reported no issues and confirmed credential safety, integrity, containment, provenance, lineage, and no temporary or unexpected remnants.
- The run is immutable and must not be reused or resumed. At the time of this probe, full Pattani ingestion had not occurred.

These findings describe one response and are not API guarantees. They do not establish stable ordering, snapshot consistency, complete coverage, duration, payload-size uniformity, or general count and termination semantics.

#### Policy-based Pattani full ingestion

Observed on **2026-09-22 UTC** as **Previously observed** project behavior:

- Run ID: `pattani-full-20260922-01`.
- The implementation reached a policy-complete terminal with stop reason `partial_page`.
- With `limit=1000`, 113 requests covered offsets `0` through `112000`; the final page contained 73 records.
- Counts were attempted `113`, persisted `113`, validated `113`, and journaled `113`.
- The run stored 112073 total features. `numberMatched` was present and consistently `112073` across the run.
- The duplicate-ID check was complete.
- Aggregate original-response size was `259102237` bytes. Aggregate credential-sanitized artifact size was `574327594` bytes.
- Lifecycle time to the terminal record was `205.031609` seconds.
- Offline verification completed with no issues, and configured-key checking was complete.
- Credential safety, containment, stored hashes, byte counts, provenance, lineage, remnant checks, and accounting passed. All referenced artifacts and metadata existed.
- Original-response hashes are recorded as provenance but cannot be independently recomputed because original response bodies are intentionally not persisted.
- No retry occurred, no response link was followed, and Git remained clean.

This is completion under the project's observed implementation policy. It does not prove exhaustive or snapshot-consistent provider coverage. Ordering, count semantics, and termination remain non-contractual. The run is immutable and must not be reused, resumed, deleted, overwritten, or repaired. Generated data remains ignored and must not be staged or committed; the repository alone is not a backup of the generated dataset.

#### Phase 2 offline validation, profiling, and transformation

Phase 2 implementation milestones:

- Phase 2A source contract: commit `bf48d94`.
- Phase 2B synthetic source validation: commit `0169c14`.
- Phase 2C offline aggregate profiler: commit `4744caf`.
- Phase 2D neutral deterministic transformation: commit `f40dd41`.

The following profiling results for run `pattani-full-20260922-01` are classified as **Previously observed**, not as an official provider contract:

- The validated input comprised 113 pages and 112,073 features.
- Credential verification was complete, and validation reported zero issues.
- All 112,073 geometry members were present, non-null objects whose observed type label was `MultiPolygon`; no missing, null, or other geometry members were observed.
- Forty property fields were observed. Every field was present and non-null in all 112,073 features.

These structural observations do not establish CRS, units, field meanings, relationships between fields, ordering, snapshot consistency, or exhaustive provider coverage. No feature IDs, coordinates, or individual property values were included in the aggregate profile.

The following Phase 2D result is **Previously observed** project execution evidence:

- Transformation ID: `neutral-jsonl-v1-20260923-01`.
- Output comprised 113 deterministic JSONL page files and one completion manifest, containing 112,073 records.
- Reported total JSONL size was `262945631` bytes.
- Completion-manifest SHA-256 was `3a901d29a66d348168deb319920cce0529092580136da09bf65c496bcddb51a2`.
- Source verification completed with zero issues. Credential, lineage, containment, integrity, and remnant checks passed.
- The transformation made zero network requests and constructed zero live API clients.
- Output is ignored by Git and is reproducible from the immutable source dataset plus committed code.

The transformation schema is a versioned project schema based on the observed snapshot, not an official GISTDA contract. It preserved source geometry without CRS assignment or reprojection and retained source attributes without semantic interpretation. No PostGIS loading, scoring, or infrastructure integration occurred.

### Credential-sanitized source contract

An active credential must never be persisted. For GISTDA flood-frequency ingestion:

- The original provider response exists only in memory.
- Its SHA-256 and byte count are recorded for provenance.
- Credential-named JSON fields and credential-bearing link query parameters are removed.
- The sanitized JSON is serialized deterministically and stored with a `.sanitized.json` suffix.
- The stored artifact's SHA-256 and byte count are recorded separately.
- The original response body is not persisted.
- Files stored under `data/raw/` for this source are sanitized source-layer artifacts, not untouched provider responses.

Sanitized source artifacts and their metadata are immutable and must never be overwritten. Their metadata must preserve retrieval context and lineage while excluding credential values, full response links, request headers, and original response bytes.

### Not yet verified

The following details are **Not yet verified**:

- API key acquisition, value format, and any prefix
- Required headers other than `API-Key`
- Complete allowed values of `pv_idn`
- Response field meanings
- Response object structure
- Geometry representation and geometry type
- Coordinate reference system (CRS)
- Pagination metadata, ordering, and end-of-results behavior
- Empty successful response behavior
- Rate limits and retry guidance
- Error-response body structure
- Any business relationship between `freq` and fields such as `y_2011` through `y_2024`

The project must not assume that `freq` equals the sum of any yearly fields unless an official GISTDA source defines that relationship.

## Later-phase data inventory

| Dataset | Purpose | Source | Format | Spatial | Time | Status |
|---|---|---|---|---|---|---|
| Hospital | Infrastructure exposure | Not yet verified | Not yet verified | Yes | No | Later phase |
| School | Infrastructure exposure | Not yet verified | Not yet verified | Yes | No | Later phase |
| Road | Transportation disruption | Not yet verified | Not yet verified | Yes | No | Later phase |
| Administrative Boundary | Geographic analysis | Not yet verified | Not yet verified | Yes | No | Later phase |
| Population | Exposure estimation | Not yet verified | Not yet verified | Yes | Yes | Later phase |
| Weather | ML features | Not yet verified | Not yet verified | Yes | Yes | Later phase |
| Elevation | Geographic feature | Not yet verified | Not yet verified | Yes | No | Later phase |

## Phase 3 infrastructure preparation

Completed on **2026-09-25 UTC** as project execution evidence:

- A dated Geofabrik Thailand OSM PBF was acquired and verified at `327676785`
  bytes, MD5 `4558c600b0e70e355c4436bd3ca80ac9`, and SHA-256
  `fc4117130af85c248c24376ba81bc593698d415f70907a994cd6813793e44b13`.
- The observed Pattani administrative boundary assembled as a valid, closed
  `MultiPolygon`. Offline extraction selected 32,324 ways with observed
  `highway` categories and produced 32,358 clipped line segments; JSONL SHA-256
  was `39a0ed9fed549e32bcf949d9cefb264ab50c957091e46698a57fc8775a372484`.
- The DGA healthcare CSV was acquired and verified. A project-defined literal
  address-text policy produced 138 Pattani candidates; JSONL SHA-256 was
  `52d87273475d9022a3656804428e0f1adfa556b8b542e1ed45694df1d7c7fb7a`.
- The DRR direct download URL remains unresolved and no DRR dataset was
  downloaded.
- Shared offline verification reported zero issues for both derived outputs.

These are observed/project results, not official completeness, accessibility,
facility-status, or positional-accuracy guarantees. OSM/OSMF licensing and
attribution/share-alike requirements apply; no legal conclusion is offered.
Official OSM documentation provides coordinate-reference evidence for OSM, but
the official GISTDA and DGA pages inspected did not establish explicit CRS
contracts. Spatial overlay remains blocked; see
`docs/SPATIAL_REFERENCE_CONTRACT.md`.

## Phase 4A exploratory intersection observation

Completed offline on **2026-09-26 UTC** under the project policy
`exploratory_non_authoritative`:

- 112,073 validated flood `MultiPolygon` features were compared with 32,358
  road segments and 138 healthcare address-text candidates.
- Geometric intersection alone marked 4,919 road segments exposed and 27,439
  non-exposed; 18 healthcare candidates exposed and 120 non-exposed.
- All 138 normal-axis DGA coordinate candidates were consistent with the
  validated Pattani boundary, while zero swapped-axis candidates were. This is
  observational consistency, not official CRS evidence.
- Input validation and the read-only exposure verifier reported zero issues.

For this run only, GISTDA geometry used a project RFC 7946 longitude/latitude
interpretation based on the documented GeoJSON endpoint and previously observed
`application/geo+json` responses. GISTDA did not explicitly state a CRS. No
field semantics, severity, accessibility, disruption, completeness, temporal
alignment, or official CRS claim is made. See
`docs/EXPLORATORY_FLOOD_EXPOSURE.md`.
