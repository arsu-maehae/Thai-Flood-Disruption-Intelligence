# Phase 6 Local Interactive Exposure Dashboard

Phase 6 adds a same-origin dashboard for the verified exploratory aggregate
service. It is a local viewer, not a public deployment or an operational flood
product. It contains no map, raw records, coordinates, feature identifiers,
source properties, database details, analytics, cookies, trackers, or external
assets.

## Start locally

Use the existing ignored Phase 5 configuration without displaying it:

```powershell
docker compose --env-file .env.phase5.local start postgis
Get-Content -LiteralPath .env.phase5.local | ForEach-Object {
  if ($_ -match '^([^#=]+)=(.*)$') {
    [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
  }
}
python -m uvicorn src.api.app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/dashboard/`. Stop the API with Ctrl+C, then stop
the database without deleting its persistent volume:

```powershell
docker compose --env-file .env.phase5.local stop postgis
```

Never use `down -v`, expose Uvicorn on `0.0.0.0`, or commit
`.env.phase5.local`.

## Display contract

The dashboard reads only the five Phase 5 GET endpoints. It presents report
provenance, road-segment and healthcare address-text-candidate headline counts,
separate 2011–2024 annual views, and observed road-category aggregates. Users
can highlight a year, filter or sort categories, and restore the default view.
Every chart has a table equivalent.

The frequency-consistency text is a version-bound observation for report
`exploratory-exposure-report-v1-20260927-01`; it is not a new API contract or a
field-semantic claim. Annual counts are not summed into unique totals, and a
zero for 2018 is not evidence that no flood occurred.

## Security and accessibility

Static assets are resolved relative to the Python module and served from three
fixed routes, so there is no directory listing or caller-controlled path.
Dashboard routes use a same-origin Content Security Policy, `no-store`, MIME
sniffing protection, frame denial, a restrictive permissions policy, and no
permissive CORS. API text is inserted with DOM text APIs rather than HTML
injection. The page provides semantic landmarks, a skip link, live status,
visible keyboard focus, responsive tables, accessible labels, and a
reduced-motion mode.

## Interpretation limits

Geometric intersection is not confirmed disruption, risk, severity, damage,
accessibility, prediction, emergency-response suitability, or complete
coverage. Road rows are segments rather than unique roads; observed categories
do not establish drivability. Healthcare rows are address-text candidates, not
verified facilities. Official GISTDA and DGA CRS, positional accuracy, field
semantics, completeness, and snapshot consistency remain unresolved.

## Verified local result

On 2026-09-27 the dashboard, stylesheet, script, and five aggregate endpoints
were exercised over loopback against the preserved real PostGIS report using a
temporary API port. Headline values reconciled to roads
`32358 / 4919 / 27439` and healthcare `138 / 18 / 120` for
total/exposed/non-exposed, with 14 ordered years and 18 road categories. The
version-bound frequency observation remained `112073 / 112073 / 0 / 0`.

HTTP/static checks confirmed local assets, restrictive response headers, safe
404 behavior, and zero external dashboard dependencies. An installed browser
automation facility loaded the real dashboard and exercised year selection,
category filtering and sorting, and keyboard activation of the reset control.
The API and database were bound only to loopback during verification.
