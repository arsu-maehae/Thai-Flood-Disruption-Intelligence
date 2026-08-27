# Data Sources

## Phase 1: Flood

Phase 1 is limited to historical flood recurrence data for Pattani Province.

### Verified from GISTDA official documentation

| Item | Verified information |
|---|---|
| Provider | GISTDA (Geo-Informatics and Space Technology Development Agency) |
| Dataset | ข้อมูลน้ำท่วมซ้ำซาก / Historical Flood Recurrence |
| Endpoint | `GET /features/flood-freq` |
| Official API documentation | [GISTDA Disaster Open API](https://disaster.gistda.or.th/services/open-api) |

The official documentation identifies the endpoint as providing a summary of recurring flood-area data. No additional API contract details are recorded here unless they have been verified from that documentation.

### Observed from our successful API test

| Item | Observed information |
|---|---|
| Geographic scope requested | Pattani Province |
| Request value used | `pv_idn=94` |
| Result | The API request completed successfully |

The successful test establishes only that the tested request with `pv_idn=94` was accepted and returned a successful response. It does not, by itself, establish an officially documented meaning or general contract for the parameter.

### Not yet verified

The following details are **Not yet verified**:

- Authentication requirements
- Required or optional headers
- Official definition and allowed values of `pv_idn`
- Response field meanings
- Coordinate reference system (CRS)
- Pagination behavior
- Rate limits
- Error-response contract

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
