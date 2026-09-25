# Spatial Reference Evidence Contract

Verified through four documentation-only GET requests on **2026-09-25 UTC**.
No dataset or API endpoint was requested.

## Official evidence

- **Official OSM community documentation:** the OpenStreetMap
  [Projection page](https://wiki.openstreetmap.org/wiki/Projection) describes
  OSM geographic coordinates using WGS 84. This evidence applies to the OSM
  source, not to GISTDA or DGA data.
- **Official IETF standard:** [RFC 7946](https://www.rfc-editor.org/rfc/rfc7946)
  defines GeoJSON coordinates in the WGS 84 geographic system and specifies
  longitude then latitude ordering. This rule applies only when RFC 7946
  conformance is established.

## Unresolved source contracts

- **GISTDA:** the official
  [Open API documentation](https://disaster.gistda.or.th/services/open-api)
  inspected on the verification date did not state an explicit CRS or RFC 7946
  conformance for the flood-frequency response. A previously observed
  `application/geo+json` response and GeoJSON-like structure do not establish
  that contract.
- **DGA healthcare:** the official
  [data.go.th catalog](https://data.go.th/th/dataset/health-citizeninfo?is_fullscreen=1)
  inspected on the verification date did not document a CRS for its coordinate
  columns.

## Project policy

The OSM-derived road coordinates may be retained according to the official OSM
coordinate evidence, without reprojection. Flood-to-road, flood-to-healthcare,
and healthcare-to-road spatial overlays remain blocked because the GISTDA and
DGA source CRS contracts are unresolved. Numeric ranges, filenames, headers,
and common practice must not be used to infer a CRS, EPSG/SRID, coordinate
order, units, or positional accuracy. Phase 4 spatial integration may begin
only after official evidence resolves each participating source contract or a
separately reviewed source-specific validation policy is approved.
