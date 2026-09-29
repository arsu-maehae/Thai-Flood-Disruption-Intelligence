"use strict";

const VERIFIED_REPORT = "exploratory-exposure-report-v1-20260927-01";
const endpoints = Object.freeze({
  health: "/health",
  metadata: "/v1/metadata",
  summary: "/v1/exposure/summary",
  annual: "/v1/exposure/annual",
  categories: "/v1/exposure/road-categories",
  spatial: "/v1/spatial/infrastructure",
});
const state = { annual: [], categories: [], selectedYear: null, map: null, mapView: { zoom: 1, x: 0, y: 0 } };
const byId = (id) => document.getElementById(id);
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const safeText = (value) => typeof value === "string" && value.length > 0 && value.length <= 1000;
const percent = (part, total) => total === 0 ? "0.0%" : `${((part / total) * 100).toFixed(1)}%`;
const formatted = (value) => new Intl.NumberFormat("en-US").format(value);

function requireObject(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid_response");
  return value;
}
function requireItems(value) {
  const object = requireObject(value);
  if (!Array.isArray(object.items)) throw new Error("invalid_response");
  return object.items;
}
function element(name, text, className) {
  const node = document.createElement(name);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function appendDefinition(list, term, description) {
  list.append(element("dt", term), element("dd", description));
}
function validateMetadata(value) {
  const item = requireObject(value);
  for (const key of ["report_id", "report_version", "policy_label", "crs_status", "interpretation_scope"]) {
    if (!safeText(item[key])) throw new Error("invalid_response");
  }
  return item;
}
function validateSummary(value) {
  const items = requireItems(value);
  if (items.length !== 2) throw new Error("invalid_response");
  for (const item of items) {
    requireObject(item);
    if (!["roads", "healthcare"].includes(item.infrastructure_type) || item.unit !== "records") throw new Error("invalid_response");
    if (![item.total_count, item.exposed_count, item.non_exposed_count].every(integer)) throw new Error("invalid_response");
    if (item.total_count !== item.exposed_count + item.non_exposed_count) throw new Error("invalid_response");
  }
  return items;
}
function validateAnnual(value) {
  const items = requireItems(value);
  if (items.length !== 28) throw new Error("invalid_response");
  for (const item of items) {
    requireObject(item);
    if (!integer(item.year) || item.year < 2011 || item.year > 2024 || !["roads", "healthcare"].includes(item.infrastructure_type) || !integer(item.exposed_count)) throw new Error("invalid_response");
  }
  const keys = items.map((item) => `${item.year}:${item.infrastructure_type}`);
  if (new Set(keys).size !== 28) throw new Error("invalid_response");
  return items.slice().sort((a, b) => a.year - b.year || a.infrastructure_type.localeCompare(b.infrastructure_type));
}
function validateCategories(value) {
  const items = requireItems(value);
  if (items.length === 0) throw new Error("invalid_response");
  for (const item of items) {
    requireObject(item);
    if (!safeText(item.road_category) || !integer(item.total_count) || !integer(item.exposed_count) || item.exposed_count > item.total_count) throw new Error("invalid_response");
  }
  return items;
}
function validateMap(value) {
  const payload = requireObject(value);
  const roads = requireObject(payload.roads); const healthcare = requireObject(payload.healthcare);
  const roadMetadata = requireObject(roads.metadata);
  const population = requireObject(roadMetadata.population_counts);
  const displayed = requireObject(roadMetadata.displayed_counts);
  if (payload.report_id !== VERIFIED_REPORT || payload.policy_label !== "exploratory_non_authoritative") throw new Error("invalid_response");
  if (!Array.isArray(payload.extent) || payload.extent.length !== 4 || !payload.extent.every(Number.isFinite)) throw new Error("invalid_response");
  if (!Array.isArray(roads.features) || !Array.isArray(healthcare.features)) throw new Error("invalid_response");
  if (![population.total, population.exposed, population.non_exposed, displayed.total, displayed.exposed, displayed.non_exposed].every(integer)) throw new Error("invalid_response");
  if (population.total !== population.exposed + population.non_exposed || displayed.total !== displayed.exposed + displayed.non_exposed || displayed.total !== roads.features.length) throw new Error("invalid_response");
  if (roadMetadata.deterministic_selection_policy !== "all_exposed_then_evenly_spaced_non_exposed_context" || roadMetadata.representative_sample !== false || roadMetadata.prevalence_inference_allowed !== false) throw new Error("invalid_response");
  if (!integer(healthcare.total_count) || !integer(healthcare.returned_count) || healthcare.returned_count !== healthcare.features.length) throw new Error("invalid_response");
  if (roads.features.length > 10000 || healthcare.features.length > 500) throw new Error("invalid_response");
  for (const feature of [...roads.features, ...healthcare.features]) {
    requireObject(feature); requireObject(feature.geometry);
    if (typeof feature.exposed !== "boolean" || !["LineString", "MultiLineString", "Point"].includes(feature.geometry.type)) throw new Error("invalid_response");
  }
  return payload;
}

function coordinateLines(geometry) {
  if (geometry.type === "LineString") return [geometry.coordinates];
  if (geometry.type === "MultiLineString") return geometry.coordinates;
  return [];
}
function renderMap() {
  if (!state.map) return;
  const canvas = byId("exposure-map"); const box = canvas.getBoundingClientRect();
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  canvas.width = Math.max(1, Math.round(box.width * ratio)); canvas.height = Math.max(1, Math.round(box.height * ratio));
  const context = canvas.getContext("2d"); context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, box.width, box.height); context.fillStyle = "#eaf1ed"; context.fillRect(0, 0, box.width, box.height);
  const [left, bottom, right, top] = state.map.extent; const padding = 18;
  const base = Math.min((box.width - padding * 2) / (right - left), (box.height - padding * 2) / (top - bottom));
  const scale = base * state.mapView.zoom; const centerX = (left + right) / 2; const centerY = (bottom + top) / 2;
  const project = (pair) => [box.width / 2 + (pair[0] - centerX) * scale + state.mapView.x,
    box.height / 2 - (pair[1] - centerY) * scale + state.mapView.y];
  const roads = state.map.roads.features.slice().sort((a, b) => Number(a.exposed) - Number(b.exposed));
  for (const road of roads) {
    context.beginPath();
    for (const line of coordinateLines(road.geometry)) {
      line.forEach((pair, index) => { const point = project(pair); if (index === 0) context.moveTo(...point); else context.lineTo(...point); });
    }
    context.strokeStyle = road.exposed ? "#c4472d" : "rgba(70, 94, 95, .42)";
    context.lineWidth = road.exposed ? 1.35 : .55; context.stroke();
  }
  for (const item of state.map.healthcare.features) {
    const point = project(item.geometry.coordinates); context.beginPath(); context.arc(point[0], point[1], item.exposed ? 4 : 2.8, 0, Math.PI * 2);
    context.fillStyle = item.exposed ? "#f4b942" : "#087f76"; context.fill();
    context.strokeStyle = "#fff"; context.lineWidth = 1; context.stroke();
  }
}
function changeMapView(zoomFactor, x, y) {
  state.mapView.zoom = Math.min(8, Math.max(1, state.mapView.zoom * zoomFactor)); state.mapView.x += x; state.mapView.y += y; renderMap();
}
function resetMap() { state.mapView = { zoom: 1, x: 0, y: 0 }; renderMap(); }
async function loadMap(reportId) {
  try {
    state.map = validateMap(await getJson(`${endpoints.spatial}?report_id=${encodeURIComponent(reportId)}`));
    renderMap();
    const roads = state.map.roads; const health = state.map.healthcare;
    const population = roads.metadata.population_counts; const displayed = roads.metadata.displayed_counts;
    byId("map-status").textContent = `All-years/ever-exposed map: displaying ${formatted(displayed.total)} of ${formatted(population.total)} road segmentsâ€”all ${formatted(displayed.exposed)} exposed roads and a bounded deterministic subset of ${formatted(displayed.non_exposed)} of ${formatted(population.non_exposed)} non-exposed roadsâ€”plus all ${formatted(health.returned_count)} healthcare candidates. Visual proportions are not prevalence; the authoritative aggregate is 15.2% exposed.`;
    for (const id of ["map-zoom-in", "map-zoom-out", "map-reset"]) byId(id).disabled = false;
  } catch (_) {
    byId("map-status").textContent = "The verified spatial view is unavailable. Aggregate charts and tables remain usable.";
    byId("map-status").classList.add("error");
  }
}
function renderMetadata(metadata) {
  const list = byId("metadata-list");
  list.replaceChildren();
  appendDefinition(list, "Report ID", metadata.report_id);
  appendDefinition(list, "Version", metadata.report_version);
  appendDefinition(list, "Policy", metadata.policy_label);
  appendDefinition(list, "CRS status", metadata.crs_status);
  appendDefinition(list, "Interpretation", metadata.interpretation_scope);
  byId("frequency-observation").textContent = metadata.report_id === VERIFIED_REPORT
    ? "For this verified report, 112,073 of 112,073 flood features matched the observed structural relationship between freq and the yearly fields; mismatches and missing/invalid values were zero. This does not establish field meaning or an official provider contract."
    : "Frequency consistency is not presented for an unrecognized report version.";
}
function renderSummary(items) {
  const target = byId("headline-cards");
  target.replaceChildren();
  for (const item of items.slice().sort((a, b) => b.total_count - a.total_count)) {
    const label = item.infrastructure_type === "roads" ? "Road segments" : "Healthcare address-text candidates";
    const card = element("article", undefined, "metric-card");
    card.append(element("h3", label));
    const list = element("dl");
    for (const [term, value] of [["Total", item.total_count], ["Exposed", item.exposed_count], ["Non-exposed", item.non_exposed_count]]) {
      const group = element("div");
      group.append(element("dt", term), element("dd", formatted(value)));
      list.append(group);
    }
    card.append(list, element("p", `${percent(item.exposed_count, item.total_count)} exposed`, "percentage"));
    target.append(card);
  }
}
function tableForAnnual(items, kind) {
  const wrapper = element("div", undefined, "table-wrap");
  const table = element("table");
  const caption = element("caption", `${kind === "roads" ? "Road-segment" : "Healthcare-candidate"} annual counts`);
  const head = element("thead"); const headerRow = element("tr");
  const yearHead = element("th", "Year"); yearHead.scope = "col";
  const countHead = element("th", "Exposed count", "numeric"); countHead.scope = "col";
  headerRow.append(yearHead, countHead); head.append(headerRow);
  const body = element("tbody");
  for (const item of items) {
    const row = element("tr"); row.dataset.year = String(item.year);
    const year = element("th", String(item.year)); year.scope = "row";
    row.append(year, element("td", formatted(item.exposed_count), "numeric")); body.append(row);
  }
  table.append(caption, head, body); wrapper.append(table); return wrapper;
}
function renderAnnual() {
  for (const kind of ["roads", "healthcare"]) {
    const items = state.annual.filter((item) => item.infrastructure_type === kind);
    const maximum = Math.max(1, ...items.map((item) => item.exposed_count));
    const chart = byId(`${kind}-chart`); chart.replaceChildren();
    for (const item of items) {
      const bar = element("span", undefined, "bar");
      const height = item.exposed_count === 0 ? 0 : Math.max(2, (item.exposed_count / maximum) * 100);
      bar.style.height = `${height}%`;
      bar.title = `${item.year}: ${formatted(item.exposed_count)}`;
      bar.setAttribute("aria-hidden", "true"); bar.dataset.year = String(item.year); chart.append(bar);
    }
    byId(`${kind}-table`).replaceChildren(tableForAnnual(items, kind));
  }
  highlightYear();
}
function highlightYear() {
  document.querySelectorAll(".bar[data-year], tbody tr[data-year]").forEach((node) => {
    const selected = Number(node.dataset.year) === state.selectedYear;
    node.classList.toggle("selected", selected); node.classList.toggle("highlight", selected);
    node.classList.toggle("dimmed", state.selectedYear !== null && !selected);
  });
}
function renderCategories() {
  const query = byId("category-filter").value.trim().toLocaleLowerCase();
  const sort = byId("category-sort").value;
  let items = state.categories.filter((item) => item.road_category.toLocaleLowerCase().includes(query));
  items = items.slice().sort((a, b) => {
    if (sort === "name-asc") return a.road_category.localeCompare(b.road_category);
    if (sort === "percentage-desc") return (b.exposed_count / Math.max(1, b.total_count)) - (a.exposed_count / Math.max(1, a.total_count)) || a.road_category.localeCompare(b.road_category);
    return b.exposed_count - a.exposed_count || a.road_category.localeCompare(b.road_category);
  });
  const wrapper = element("div", undefined, "table-wrap"); const table = element("table");
  const caption = element("caption", `${items.length} observed road categories`);
  const head = element("thead"); const header = element("tr");
  for (const [label, numeric] of [["Category", false], ["Total segments", true], ["Exposed segments", true], ["Exposed percentage", true]]) {
    const cell = element("th", label, numeric ? "numeric" : undefined); cell.scope = "col"; header.append(cell);
  }
  head.append(header); const body = element("tbody");
  for (const item of items) {
    const row = element("tr"); const name = element("th", item.road_category); name.scope = "row";
    row.append(name, element("td", formatted(item.total_count), "numeric"), element("td", formatted(item.exposed_count), "numeric"), element("td", percent(item.exposed_count, item.total_count), "numeric")); body.append(row);
  }
  if (items.length === 0) { const row = element("tr"); const cell = element("td", "No categories match this filter."); cell.colSpan = 4; row.append(cell); body.append(row); }
  table.append(caption, head, body); wrapper.append(table); byId("category-table").replaceChildren(wrapper);
}
async function getJson(url) {
  const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error("request_failed");
  return response.json();
}
async function loadDashboard() {
  try {
    const health = requireObject(await getJson(endpoints.health));
    if (health.status !== "ok") throw new Error("service_unavailable");
    byId("health-pill").textContent = "Local service ready";
    const metadata = validateMetadata(await getJson(endpoints.metadata));
    const query = `?report_id=${encodeURIComponent(metadata.report_id)}`;
    const [summary, annual, categories] = await Promise.all([getJson(`${endpoints.summary}${query}`), getJson(`${endpoints.annual}${query}`), getJson(`${endpoints.categories}${query}`)]);
    state.annual = validateAnnual(annual); state.categories = validateCategories(categories);
    renderMetadata(metadata); renderSummary(validateSummary(summary));
    const yearSelect = byId("year-select"); yearSelect.append(new Option("All years", ""));
    for (let year = 2011; year <= 2024; year += 1) yearSelect.append(new Option(String(year), String(year)));
    renderAnnual(); renderCategories();
    for (const id of ["year-select", "category-filter", "category-sort", "reset-controls"]) byId(id).disabled = false;
    byId("status").textContent = "Verified aggregate view loaded.";
    await loadMap(metadata.report_id);
  } catch (_) {
    byId("health-pill").textContent = "Local service unavailable";
    byId("status").textContent = "The aggregate view could not be loaded. Check the local service and try again.";
    byId("status").classList.add("error");
  }
}
byId("year-select").addEventListener("change", (event) => {
  state.selectedYear = event.target.value === "" ? null : Number(event.target.value); highlightYear();
  byId("map-year-note").textContent = state.selectedYear === null
    ? "This is an all-years/ever-exposed map. Feature-level annual masks were not persisted, so year selection applies to the charts and tables only and never reclassifies or filters map features."
    : `${state.selectedYear} is highlighted in the charts and tables only. The map remains the unchanged all-years/ever-exposed view; it does not represent ${state.selectedYear}.`;
});
byId("map-zoom-in").addEventListener("click", () => changeMapView(1.35, 0, 0));
byId("map-zoom-out").addEventListener("click", () => changeMapView(1 / 1.35, 0, 0));
byId("map-reset").addEventListener("click", resetMap);
byId("exposure-map").addEventListener("keydown", (event) => {
  const actions = { ArrowLeft: [1, 28, 0], ArrowRight: [1, -28, 0], ArrowUp: [1, 0, 28], ArrowDown: [1, 0, -28], "+": [1.35, 0, 0], "=": [1.35, 0, 0], "-": [1 / 1.35, 0, 0] };
  if (event.key === "Home") { event.preventDefault(); resetMap(); return; }
  if (actions[event.key]) { event.preventDefault(); changeMapView(...actions[event.key]); }
});
new ResizeObserver(renderMap).observe(byId("exposure-map"));
byId("category-filter").addEventListener("input", renderCategories);
byId("category-sort").addEventListener("change", renderCategories);
byId("reset-controls").addEventListener("click", () => {
  state.selectedYear = null; byId("year-select").value = ""; byId("category-filter").value = ""; byId("category-sort").value = "exposed-desc";
  highlightYear(); renderCategories(); byId("year-select").focus();
});
loadDashboard();
