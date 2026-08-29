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
