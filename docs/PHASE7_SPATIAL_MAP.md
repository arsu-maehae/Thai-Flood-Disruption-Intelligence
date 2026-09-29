# Phase 7 Local Exploratory Spatial Map

Phase 7 adds a bounded, read-only spatial view to the local Phase 6 dashboard. It uses only the immutable Phase 3 infrastructure outputs and the verified Phase 4A exposure classifications. No source acquisition, external tile service, or browser-side provider data is involved.

## Start and stop

Use the existing ignored Phase 5 environment file without printing it:

```powershell
docker compose --env-file .env.phase5.local start postgis
$settings = Get-Content .env.phase5.local | Where-Object { $_ -and -not $_.StartsWith('#') }
foreach ($setting in $settings) { $name, $value = $setting -split '=', 2; [Environment]::SetEnvironmentVariable($name, $value, 'Process') }
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/dashboard/`. Stop Uvicorn with `Ctrl+C`, then preserve the database volume:

```powershell
docker compose --env-file .env.phase5.local stop postgis
```

Never use `docker compose down -v` for this project.

## Spatial interface and limits

`GET /v1/spatial/infrastructure?report_id=exploratory-exposure-report-v1-20260927-01` returns a deterministic same-origin payload. It verifies the referenced Phase 3 and Phase 4 outputs before serving:

- all 4,919 exposed road segments;
- a bounded deterministic subset of 5,081 of 27,439 non-exposed road segments, for 10,000 displayed roads total;
- all 138 healthcare address-text candidates, including 18 classified as exposed;
- no source identifiers, source links, flood polygons, properties, filesystem paths, or database details.

The road payload records population counts `32,358 / 4,919 / 27,439` and displayed counts `10,000 / 4,919 / 5,081` for total/exposed/non-exposed. Its deterministic selection policy explicitly declares `representative_sample: false` and `prevalence_inference_allowed: false`. Visual proportions must not be interpreted as exposure prevalence; the verified aggregate remains 15.2% exposed.

Hard limits are 10,000 road features, 500 healthcare features, and 8 MiB of canonical JSON. Extra, missing, duplicate, or invalid query parameters are rejected. The endpoint is read-only and the dashboard uses browser-native Canvas with no CDN, external tiles, cookies, trackers, or permissive CORS.

## Interaction and accessibility

Use the buttons or `+`/`-` to zoom, arrow keys to pan, and `Home` to reset. The Canvas has a descriptive accessible label and visible focus styling. Layout is responsive and reduced-motion preferences remain honored. Loading and fixed safe failure states do not expose internal errors.

The map is labeled all-years/ever-exposed. The existing year selector continues to highlight annual aggregate charts and tables, but it never filters or reclassifies map features because feature-level annual masks were not persisted; silently reconstructing them would break the reviewed lineage boundary.

## Interpretation limits

The map retains the Phase 4 project policy of interpreting source positions according to RFC 7946 for exploratory comparison only. This is not an official GISTDA or DGA CRS determination. No flood polygons are sent to the browser. Road labels do not establish drivability, healthcare rows remain address-text candidates, and geometric intersection does not establish disruption, damage, risk, severity, accessibility, positional accuracy, completeness, or snapshot consistency.
