# Pattani OSM Road Extraction Method

This project-defined offline transformation uses the reviewed dated Geofabrik
Thailand OSM PBF and the single previously observed Pattani administrative
relation. It does not establish an official administrative-boundary contract,
road completeness, stable OSM ordering, or snapshot consistency.

The transformation requires the observed relation structure and its assembled,
valid, closed `MultiPolygon` to remain unchanged. PyOsmium uses its `flex_mem`
location index under explicit source-size and available-memory safety caps;
Shapely intersects each OSM way carrying a non-empty
`highway` tag with the actual boundary geometry. Crossing ways are clipped and
disjoint line components are retained. No geometry repair, simplification,
reprojection, CRS assignment, access inference, road-condition inference, or
drivability classification is performed.

Each deterministic JSONL record contains a sequence, source OSM way provenance,
the observed `highway` category, and one clipped `LineString`. The immutable
completion manifest records source and boundary lineage, pinned tool and schema
versions, resource limits, category/count aggregates, output byte count, and
SHA-256. Output is published without replacement and the manifest is published
last. A run without that manifest is incomplete and must not be repaired,
overwritten, or treated as complete.

The implementation enforces explicit source-size, available-memory, free-space,
output-byte, and output-record limits. Generated output remains Git-ignored and
reproducible from the immutable source artifact plus committed code.
