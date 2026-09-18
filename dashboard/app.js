const AUTH_TOKEN_STORAGE_KEY = "aisleiq_token";

const state = {
  storeId: "ST1001",
  comparisonStores: ["ST1001", "ST1002"],
  // P4.4: kept in sessionStorage (not localStorage) so a token doesn't
  // outlive the browser tab/session it was issued in.
  authToken: sessionStorage.getItem(AUTH_TOKEN_STORAGE_KEY),
  metrics: null,
  funnel: null,
  heatmap: null,
  insights: null,
  paths: null,
  // Live Analytics (P4.2) is loaded lazily -- see ensureLiveAnalyticsLoaded --
  // so opening the dashboard and never visiting that tab fires none of its
  // 8 requests. analyticsRange is only meaningful once loaded.
  liveAnalyticsLoaded: false,
  analyticsRange: null,
  // Zone Map (P8) is loaded lazily the first time its tab opens, same pattern
  // as Live Analytics above.
  zoneMapLoaded: false,
};

const RANGE_PRESET_HOURS = { "24h": 24, "3d": 72, "7d": 168 };
const RANGE_PRESET_LABELS = {
  "24h": "Last 24 hours",
  "3d": "Last 3 days",
  "7d": "Last 7 days",
  custom: "Custom range",
};

const elements = {
  loginGate: document.querySelector("#login-gate"),
  loginForm: document.querySelector("#login-form"),
  loginEmail: document.querySelector("#login-email"),
  loginPassword: document.querySelector("#login-password"),
  loginError: document.querySelector("#login-error"),
  appShell: document.querySelector("#app-shell"),
  logoutButton: document.querySelector("#logout-button"),
  pageTitle: document.querySelector("#page-title"),
  storeForm: document.querySelector("#store-form"),
  storeInput: document.querySelector("#store-id"),
  statusBanner: document.querySelector("#status-banner"),
  kpiGrid: document.querySelector("#kpi-grid"),
  zoneDwellBody: document.querySelector("#zone-dwell-body"),
  zoneCount: document.querySelector("#zone-count"),
  attributionCount: document.querySelector("#attribution-count"),
  revenueLarge: document.querySelector("#revenue-large"),
  funnelTrack: document.querySelector("#funnel-track"),
  funnelStoreLabel: document.querySelector("#funnel-store-label"),
  pathsCount: document.querySelector("#paths-count"),
  commonPaths: document.querySelector("#common-paths"),
  purchasePaths: document.querySelector("#purchase-paths"),
  dropoffPaths: document.querySelector("#dropoff-paths"),
  insightsCount: document.querySelector("#insights-count"),
  insightsList: document.querySelector("#insights-list"),
  comparisonGrid: document.querySelector("#comparison-grid"),
  heatmapStage: document.querySelector("#heatmap-stage"),
  heatmapSummary: document.querySelector("#heatmap-summary"),
  tabButtons: document.querySelectorAll(".tab-button"),
  viewPanels: document.querySelectorAll("[data-view-panel]"),
  analyticsAsOf: document.querySelector("#analytics-as-of"),
  rangePreset: document.querySelector("#range-preset"),
  rangeCustom: document.querySelector("#range-custom"),
  rangeStart: document.querySelector("#range-start"),
  rangeEnd: document.querySelector("#range-end"),
  rangeApply: document.querySelector("#range-apply"),
  rangeError: document.querySelector("#range-error"),
  analyticsEmpty: document.querySelector("#analytics-empty"),
  analyticsContent: document.querySelector("#analytics-content"),
  analyticsKpiGrid: document.querySelector("#analytics-kpi-grid"),
  occupancyHistorySummary: document.querySelector("#occupancy-history-summary"),
  occupancyHistoryChart: document.querySelector("#occupancy-history-chart"),
  footfallSummary: document.querySelector("#footfall-summary"),
  footfallChart: document.querySelector("#footfall-chart"),
  queueActivitySummary: document.querySelector("#queue-activity-summary"),
  queueActivityChart: document.querySelector("#queue-activity-chart"),
  peakHoursSummary: document.querySelector("#peak-hours-summary"),
  peakHoursList: document.querySelector("#peak-hours-list"),
  comparisonPeriodLabel: document.querySelector("#comparison-period-label"),
  periodComparisonGrid: document.querySelector("#period-comparison-grid"),
  whatChangedBanner: document.querySelector("#what-changed-banner"),
  alertsSummary: document.querySelector("#alerts-summary"),
  alertsList: document.querySelector("#alerts-list"),
  zoneMapMetric: document.querySelector("#zone-map-metric"),
  zoneMapEmpty: document.querySelector("#zone-map-empty"),
  zoneMapEmptyMessage: document.querySelector("#zone-map-empty-message"),
  zoneMapContent: document.querySelector("#zone-map-content"),
  zoneMapStage: document.querySelector("#zone-map-stage"),
  zoneMapCount: document.querySelector("#zone-map-count"),
  zoneMapList: document.querySelector("#zone-map-list"),
  replayForm: document.querySelector("#replay-form"),
  replaySourceType: document.querySelector("#replay-source-type"),
  replayDatasetField: document.querySelector("#replay-dataset-field"),
  replayDataset: document.querySelector("#replay-dataset"),
  replayStart: document.querySelector("#replay-start"),
  replayEnd: document.querySelector("#replay-end"),
  replayRunButton: document.querySelector("#replay-run"),
  replayError: document.querySelector("#replay-error"),
  replayResultPanel: document.querySelector("#replay-result-panel"),
  replayResultStatus: document.querySelector("#replay-result-status"),
  replayResultKpis: document.querySelector("#replay-result-kpis"),
  replayResultErrors: document.querySelector("#replay-result-errors"),
  replayJobsCount: document.querySelector("#replay-jobs-count"),
  replayJobsBody: document.querySelector("#replay-jobs-body"),
};

const MAX_VISIBLE_ALERTS = 4;

const metricCards = [
  ["Total Visitors", "total_visitors", formatNumber, "Customer visit sessions"],
  ["Unique Visitors", "unique_visitors", formatNumber, "Distinct tracked visitors"],
  ["Staff Filtered", "staff_visitors", formatNumber, "Inferred staff excluded from customer metrics"],
  ["Solo Visitors", "solo_visitors", formatNumber, "Customers not assigned to a group"],
  ["Groups", "groups", formatNumber, "Detected shopping groups"],
  ["Avg Group Size", "average_group_size", formatDecimal, "Mean shoppers per detected group"],
  ["Conversion Rate", "conversion_rate", formatPercent, "Matched purchases per visitor"],
  ["Revenue", "attributed_revenue", formatCurrency, "Matched POS revenue"],
  ["Average Dwell", "average_dwell_seconds", formatDuration, "Completed customer sessions"],
  ["Queue Abandonment", "queue_abandonment_rate", formatPercent, "Queue exits without completion"],
];

elements.tabButtons.forEach((button) => {
  button.addEventListener("click", () => activateView(button.dataset.view));
});

elements.storeForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const previousStoreId = state.storeId;
  state.storeId = elements.storeInput.value.trim().toUpperCase() || "ST1001";
  state.comparisonStores = Array.from(new Set([state.storeId, "ST1002"]));
  if (state.storeId !== previousStoreId) {
    // A different store's available data may not suit a previously chosen
    // custom range -- fall back to the default preset rather than carrying
    // a stale selection across stores. A plain Refresh (same store) leaves
    // the user's chosen range alone.
    resetAnalyticsRangeControl();
  }
  loadDashboard();
  refreshLiveAnalyticsIfLoaded();
  refreshZoneMapIfLoaded();
});

elements.rangePreset.addEventListener("change", () => {
  const isCustom = elements.rangePreset.value === "custom";
  elements.rangeCustom.hidden = !isCustom;
  elements.rangeError.hidden = true;
  if (!isCustom) {
    loadLiveAnalytics();
  }
});

elements.zoneMapMetric.addEventListener("change", () => {
  loadZoneMap();
});

elements.replaySourceType.addEventListener("change", () => {
  elements.replayDatasetField.hidden = elements.replaySourceType.value !== "jsonl_file";
});

elements.replayForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runReplay();
});

elements.rangeApply.addEventListener("click", () => {
  const start = new Date(elements.rangeStart.value);
  const end = new Date(elements.rangeEnd.value);
  if (!isValidRange(start, end)) {
    elements.rangeError.hidden = false;
    elements.rangeError.textContent = "Enter a start and end, with end after start.";
    return;
  }
  elements.rangeError.hidden = true;
  loadLiveAnalytics();
});

elements.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.loginError.hidden = true;
  try {
    const response = await fetch("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: elements.loginEmail.value.trim(),
        password: elements.loginPassword.value,
      }),
    });
    if (!response.ok) {
      throw new Error("Invalid email or password.");
    }
    const body = await response.json();
    state.authToken = body.access_token;
    sessionStorage.setItem(AUTH_TOKEN_STORAGE_KEY, state.authToken);
    elements.loginPassword.value = "";
    showAppShell();
  } catch (error) {
    elements.loginError.hidden = false;
    elements.loginError.textContent = error.message;
  }
});

elements.logoutButton.addEventListener("click", () => {
  showLoginGate();
});

function showLoginGate() {
  state.authToken = null;
  sessionStorage.removeItem(AUTH_TOKEN_STORAGE_KEY);
  elements.appShell.hidden = true;
  elements.loginGate.hidden = false;
}

function showAppShell() {
  elements.loginGate.hidden = true;
  elements.appShell.hidden = false;
  loadDashboard();
  refreshLiveAnalyticsIfLoaded();
  refreshZoneMapIfLoaded();
}

if (state.authToken) {
  showAppShell();
} else {
  showLoginGate();
}

async function loadDashboard() {
  setStatus("");
  try {
    const [metrics, funnel, heatmap, insights, paths] = await Promise.all([
      fetchJson(`/stores/${state.storeId}/metrics`),
      fetchJson(`/stores/${state.storeId}/funnel`),
      fetchJson(`/stores/${state.storeId}/heatmap`),
      fetchJson(`/stores/${state.storeId}/insights`),
      fetchJson(`/stores/${state.storeId}/paths`),
    ]);
    state.metrics = metrics;
    state.funnel = funnel;
    state.heatmap = heatmap;
    state.insights = insights;
    state.paths = paths;
    renderOverview(metrics);
    renderFunnel(funnel);
    renderHeatmap(heatmap);
    renderInsights(insights);
    renderPaths(paths);
    await renderComparison();
  } catch (error) {
    renderEmptyState();
    setStatus(error.message);
  }
}

async function fetchJson(path) {
  const headers = { Accept: "application/json" };
  if (state.authToken) {
    headers.Authorization = `Bearer ${state.authToken}`;
  }
  const response = await fetch(path, { headers });
  if (response.status === 401) {
    // Token missing/expired/invalid -- drop it and show the login gate
    // instead of surfacing a raw fetch error through every panel.
    showLoginGate();
    throw new Error("Session expired. Please sign in again.");
  }
  if (!response.ok) {
    throw new Error(`Unable to load ${path}: ${response.status}`);
  }
  return response.json();
}

async function postJson(path, body) {
  const headers = { Accept: "application/json", "Content-Type": "application/json" };
  if (state.authToken) {
    headers.Authorization = `Bearer ${state.authToken}`;
  }
  const response = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  if (response.status === 401) {
    showLoginGate();
    throw new Error("Session expired. Please sign in again.");
  }
  if (!response.ok) {
    let message = `Request failed: ${response.status}`;
    try {
      const errorBody = await response.json();
      message = errorBody.detail || errorBody.message || message;
    } catch {
      // response body wasn't JSON -- keep the generic message
    }
    throw new Error(message);
  }
  return response.json();
}

function activateView(viewName) {
  const titles = { comparison: "Store Comparison", "live-analytics": "Live Analytics", "zone-map": "Zone Map", replay: "Replay" };
  elements.pageTitle.textContent = titles[viewName] || titleCase(viewName);
  elements.tabButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === viewName);
  });
  elements.viewPanels.forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.viewPanel === viewName);
  });
  if (viewName === "live-analytics") {
    ensureLiveAnalyticsLoaded();
  }
  if (viewName === "zone-map") {
    ensureZoneMapLoaded();
  }
  if (viewName === "replay") {
    loadReplayJobs();
  }
}

function renderOverview(metrics) {
  elements.kpiGrid.innerHTML = metricCards
    .map(([label, key, formatter, caption]) => {
      return `
        <article class="kpi-card">
          <span>${label}</span>
          <strong>${formatter(metrics[key])}</strong>
          <small>${caption}</small>
        </article>
      `;
    })
    .join("");

  const zones = metrics.zone_dwell_metrics || [];
  elements.zoneCount.textContent = `${zones.length} ${zones.length === 1 ? "zone" : "zones"}`;
  elements.zoneDwellBody.innerHTML = zones.length
    ? zones
        .map((zone) => {
          return `
            <tr>
              <td>${escapeHtml(zone.zone_id)}</td>
              <td>${formatNumber(zone.visits)}</td>
              <td>${formatDuration(zone.average_dwell_seconds)}</td>
              <td>${formatDuration(zone.total_dwell_seconds)}</td>
            </tr>
          `;
        })
        .join("")
    : `<tr><td class="empty-row" colspan="4">No zone dwell data available.</td></tr>`;

  elements.attributionCount.textContent = `${formatNumber(metrics.attributed_transactions)} transactions`;
  elements.revenueLarge.textContent = formatCurrency(metrics.attributed_revenue);
}

function renderFunnel(funnel) {
  elements.funnelStoreLabel.textContent = funnel.store_id;
  elements.funnelTrack.innerHTML = funnel.steps
    .map((step) => {
      const width = Math.round((step.rate_from_visitors ?? 0) * 100);
      return `
        <article class="funnel-step">
          <div>
            <span>${titleCase(step.step.replace("_", " "))}</span>
            <strong>${formatNumber(step.count)}</strong>
          </div>
          <div>
            <div class="progress-bar" aria-label="${escapeHtml(step.step)} share">
              <i style="--bar-width: ${width}%"></i>
            </div>
            <small>${formatPercent(step.rate_from_previous)} from previous</small>
          </div>
        </article>
      `;
    })
    .join("");
}

function renderHeatmap(heatmap) {
  const points = heatmap.points || [];
  elements.heatmapSummary.textContent = `${formatNumber(heatmap.point_count)} points / ${formatNumber(heatmap.total_events)} events`;

  if (!heatmap.layout_image_url || !points.length) {
    elements.heatmapStage.innerHTML = `<div class="heatmap-empty">No heatmap data available.</div>`;
    return;
  }

  const pointMarkup = points
    .map((point) => {
      const intensity = Math.max(0, Math.min(1, point.intensity || 0));
      const diameter = 18 + intensity * 42;
      const opacity = 0.38 + intensity * 0.52;
      const hue = 44 - intensity * 34;

      return `
        <span
          class="heatmap-point"
          title="${formatNumber(point.count)} events"
          style="
            --x: ${Math.max(0, Math.min(1, point.x || 0)) * 100}%;
            --y: ${Math.max(0, Math.min(1, point.y || 0)) * 100}%;
            --diameter: ${diameter.toFixed(1)}px;
            --opacity: ${opacity.toFixed(2)};
            --point-color: hsl(${hue.toFixed(0)} 84% 52%);
          "
        ></span>
      `;
    })
    .join("");

  elements.heatmapStage.innerHTML = `
    <img class="heatmap-layout" src="${escapeHtml(heatmap.layout_image_url)}" alt="${escapeHtml(heatmap.store_id)} store layout">
    <div class="heatmap-layer" aria-label="${escapeHtml(heatmap.store_id)} heatmap overlay">
      ${pointMarkup}
    </div>
  `;
}

function renderInsights(response) {
  const insights = response.insights || [];
  elements.insightsCount.textContent = `${formatNumber(insights.length)} ${insights.length === 1 ? "insight" : "insights"}`;
  elements.insightsList.innerHTML = insights.length
    ? insights
        .map((insight) => {
          return `
            <article class="insight-card severity-${escapeHtml(insight.severity.toLowerCase())}">
              <header>
                <span>${escapeHtml(insight.severity)}</span>
                <small>${escapeHtml(insight.category)}</small>
              </header>
              <h4>${escapeHtml(insight.title)}</h4>
              <p>${escapeHtml(insight.explanation)}</p>
              <strong>${escapeHtml(insight.recommendation)}</strong>
            </article>
          `;
        })
        .join("")
    : `<div class="empty-row">No actionable insights for the current store.</div>`;
}

function renderPaths(response) {
  elements.pathsCount.textContent = `${formatNumber(response.total_journeys)} journeys`;
  renderPathList(elements.commonPaths, response.most_common_paths || []);
  renderPathList(elements.purchasePaths, response.top_purchase_journeys || []);
  renderPathList(elements.dropoffPaths, response.path_drop_offs || []);
}

function renderPathList(container, paths) {
  container.innerHTML = paths.length
    ? paths
        .map((path) => {
          return `
            <article class="path-card">
              <strong>${path.path.map(escapeHtml).join(" -> ")}</strong>
              <dl>
                <div><dt>Visitors</dt><dd>${formatNumber(path.visitor_count)}</dd></div>
                <div><dt>Conversion</dt><dd>${formatPercent(path.conversion_rate)}</dd></div>
                <div><dt>Drop-off</dt><dd>${formatPercent(path.abandonment_rate)}</dd></div>
              </dl>
            </article>
          `;
        })
        .join("")
    : `<div class="empty-row">No path data available.</div>`;
}

async function renderComparison() {
  const stores = await Promise.all(
    state.comparisonStores.map(async (storeId) => {
      try {
        return await fetchJson(`/stores/${storeId}/metrics`);
      } catch {
        return {
          store_id: storeId,
          total_visitors: 0,
          conversion_rate: 0,
          attributed_revenue: 0,
          average_dwell_seconds: 0,
          queue_abandonment_rate: 0,
          staff_visitors: 0,
          solo_visitors: 0,
          groups: 0,
          average_group_size: 0,
        };
      }
    })
  );

  elements.comparisonGrid.innerHTML = stores
    .map((store) => {
      return `
        <article class="comparison-card">
          <header>
            <h3>${escapeHtml(store.store_id)}</h3>
            <span>${formatPercent(store.conversion_rate)}</span>
          </header>
          <dl>
            <div><dt>Visitors</dt><dd>${formatNumber(store.total_visitors)}</dd></div>
            <div><dt>Revenue</dt><dd>${formatCurrency(store.attributed_revenue)}</dd></div>
            <div><dt>Avg dwell</dt><dd>${formatDuration(store.average_dwell_seconds)}</dd></div>
            <div><dt>Queue abandon</dt><dd>${formatPercent(store.queue_abandonment_rate)}</dd></div>
          </dl>
        </article>
      `;
    })
    .join("");
}

// ---------------------------------------------------------------------------
// Live Analytics (P4.2) -- request/response only, no polling or streaming.
// Loaded lazily (see activateView) the first time the tab is opened, then
// refreshed on store change, range change, and the existing Refresh button.
// ---------------------------------------------------------------------------

function ensureLiveAnalyticsLoaded() {
  if (state.liveAnalyticsLoaded) {
    return;
  }
  state.liveAnalyticsLoaded = true;
  loadLiveAnalytics();
}

function refreshLiveAnalyticsIfLoaded() {
  if (state.liveAnalyticsLoaded) {
    loadLiveAnalytics();
  }
}

function resetAnalyticsRangeControl() {
  elements.rangePreset.value = "24h";
  elements.rangeCustom.hidden = true;
  elements.rangeError.hidden = true;
}

async function loadLiveAnalytics() {
  setStatus("");
  try {
    const health = await fetchJson("/health");
    const storeHealth = (health.stores || {})[state.storeId];

    if (!storeHealth || !storeHealth.last_event_timestamp) {
      // Honest empty state: this store has no recorded activity at all, so
      // none of the 8 analytics endpoints have anything to answer -- show
      // one clear message instead of firing (and failing to render) eight
      // requests against a store with nothing ingested yet.
      setLiveAnalyticsAvailability(false);
      elements.analyticsAsOf.textContent = "As of — no recorded activity yet";
      return;
    }

    const asOf = new Date(storeHealth.last_event_timestamp);
    elements.analyticsAsOf.textContent = `As of ${formatDateTime(asOf)}`;

    const range = resolveAnalyticsRange(asOf);
    state.analyticsRange = range;
    setLiveAnalyticsAvailability(true);

    const query = rangeParams(range);
    const [occCurrent, queueCurrent, queueMetrics, occHistory, footfall, queueHourly, peak, comparison, anomalies] =
      await Promise.all([
        fetchJson(`/stores/${state.storeId}/occupancy/current`),
        fetchJson(`/stores/${state.storeId}/queue/current`),
        fetchJson(`/stores/${state.storeId}/queue/metrics?${query}`),
        fetchJson(`/stores/${state.storeId}/occupancy/history?${query}`),
        fetchJson(`/stores/${state.storeId}/footfall/hourly?${query}`),
        fetchJson(`/stores/${state.storeId}/queue/hourly?${query}`),
        fetchJson(`/stores/${state.storeId}/peak-hours?${query}`),
        fetchJson(`/stores/${state.storeId}/comparison?${query}`),
        // Not one of the 8 P3 endpoints -- a pre-existing (pre-P3) endpoint
        // that P4.3 extends with the same optional start/end convention.
        fetchJson(`/stores/${state.storeId}/anomalies?${query}`),
      ]);

    renderAnalyticsKpis(occCurrent, queueCurrent, queueMetrics);
    renderOccupancyHistory(occHistory);
    renderHourlyFootfall(footfall);
    renderQueueActivityChart(queueHourly);
    renderPeakHours(peak);
    renderPeriodComparison(comparison);
    renderAlerts(anomalies);
    renderWhatChanged(anomalies);
  } catch (error) {
    setStatus(error.message);
  }
}

function resolveAnalyticsRange(asOf) {
  const preset = elements.rangePreset.value;
  if (preset === "custom") {
    const start = new Date(elements.rangeStart.value);
    const end = new Date(elements.rangeEnd.value);
    if (isValidRange(start, end)) {
      return { start, end, preset: "custom" };
    }
    // Defensive fallback only -- the Apply button already validates before
    // ever calling loadLiveAnalytics with preset "custom".
  }
  const hours = RANGE_PRESET_HOURS[preset] || RANGE_PRESET_HOURS["24h"];
  return { start: new Date(asOf.getTime() - hours * 60 * 60 * 1000), end: asOf, preset: preset in RANGE_PRESET_HOURS ? preset : "24h" };
}

function isValidRange(start, end) {
  return start instanceof Date && !isNaN(start) && end instanceof Date && !isNaN(end) && end > start;
}

function rangeParams(range) {
  return `start=${encodeURIComponent(range.start.toISOString())}&end=${encodeURIComponent(range.end.toISOString())}`;
}

function setLiveAnalyticsAvailability(available) {
  elements.analyticsContent.hidden = !available;
  elements.analyticsEmpty.hidden = available;
}

function renderAnalyticsKpis(occCurrent, queueCurrent, queueMetrics) {
  const rangeLabel = RANGE_PRESET_LABELS[state.analyticsRange.preset] || "selected range";
  const hasQueueVolume = queueMetrics.completed_visits + queueMetrics.abandoned_visits > 0;

  elements.analyticsKpiGrid.innerHTML = `
    <article class="kpi-card">
      <span>Current Occupancy</span>
      <strong>${formatNumber(occCurrent.occupancy)}</strong>
      <small>As of ${formatDateTime(new Date(occCurrent.as_of))}</small>
    </article>
    <article class="kpi-card">
      <span>Current Queue Length</span>
      <strong>${formatNumber(queueCurrent.queue_length)}</strong>
      <small>As of ${formatDateTime(new Date(queueCurrent.as_of))}</small>
    </article>
    <article class="kpi-card">
      <span>Avg Wait Time</span>
      <strong>${queueMetrics.average_wait_seconds === null ? "—" : formatDuration(queueMetrics.average_wait_seconds)}</strong>
      <small>${queueMetrics.average_wait_seconds === null ? "No completed visits in range" : rangeLabel}</small>
    </article>
    <article class="kpi-card">
      <span>Queue Abandonment Rate</span>
      <strong>${hasQueueVolume ? formatPercent(queueMetrics.abandonment_rate) : "—"}</strong>
      <small>${hasQueueVolume ? rangeLabel : "No queue visits in range"}</small>
    </article>
  `;
}

function renderBarChart(container, values, labels, emptyMessage) {
  const hasActivity = values.some((value) => value > 0);
  if (!values.length || !hasActivity) {
    container.innerHTML = `<div class="bar-chart-empty">${escapeHtml(emptyMessage)}</div>`;
    return;
  }
  const max = Math.max(...values, 1);
  container.innerHTML = values
    .map((value, index) => {
      const heightPct = Math.round((value / max) * 100);
      return `<span class="bar-chart-bar" style="--bar-height: ${heightPct}%" title="${escapeHtml(labels[index])}: ${formatNumber(value)}"></span>`;
    })
    .join("");
}

function renderOccupancyHistory(response) {
  const points = response.points || [];
  const values = points.map((point) => point.occupancy);
  const labels = points.map((point) => formatBucketLabel(point.bucket_start));
  const max = values.length ? Math.max(...values) : 0;

  elements.occupancyHistorySummary.textContent = max > 0 ? `Peak ${formatNumber(max)}` : "No occupancy in range";
  renderBarChart(elements.occupancyHistoryChart, values, labels, "No occupancy recorded in this range.");
}

function renderHourlyFootfall(response) {
  const buckets = response.buckets || [];
  const values = buckets.map((bucket) => bucket.entries);
  const labels = buckets.map((bucket) => formatBucketLabel(bucket.bucket_start));
  const total = values.reduce((sum, value) => sum + value, 0);

  elements.footfallSummary.textContent = total > 0 ? `${formatNumber(total)} entries` : "No entries in range";
  renderBarChart(elements.footfallChart, values, labels, "No entries recorded in this range.");
}

function renderQueueActivityChart(response) {
  const buckets = response.buckets || [];
  const totalJoined = buckets.reduce((sum, bucket) => sum + bucket.joined, 0);
  const totalCompleted = buckets.reduce((sum, bucket) => sum + bucket.completed, 0);
  const totalAbandoned = buckets.reduce((sum, bucket) => sum + bucket.abandoned, 0);

  elements.queueActivitySummary.textContent =
    totalJoined + totalCompleted + totalAbandoned > 0
      ? `${formatNumber(totalCompleted)} completed · ${formatNumber(totalAbandoned)} abandoned`
      : "No queue activity in range";

  if (!buckets.length || totalJoined + totalCompleted + totalAbandoned === 0) {
    elements.queueActivityChart.innerHTML = `<div class="bar-chart-empty">No queue activity recorded in this range.</div>`;
    return;
  }

  const max = Math.max(...buckets.flatMap((bucket) => [bucket.joined, bucket.completed, bucket.abandoned]), 1);
  elements.queueActivityChart.innerHTML = buckets
    .map((bucket) => {
      const label = formatBucketLabel(bucket.bucket_start);
      return `
        <span class="bar-chart-group" title="${escapeHtml(label)}">
          <i class="bar-chart-bar bar-chart-bar--joined" style="--bar-height: ${Math.round((bucket.joined / max) * 100)}%"></i>
          <i class="bar-chart-bar bar-chart-bar--completed" style="--bar-height: ${Math.round((bucket.completed / max) * 100)}%"></i>
          <i class="bar-chart-bar bar-chart-bar--abandoned" style="--bar-height: ${Math.round((bucket.abandoned / max) * 100)}%"></i>
        </span>
      `;
    })
    .join("");
}

function renderPeakHours(response) {
  const ranked = response.ranked_hours || [];

  elements.peakHoursSummary.textContent = response.peak_hour
    ? `Peak ${formatBucketLabel(response.peak_hour.bucket_start)}`
    : "No peak in range";

  if (!response.peak_hour) {
    elements.peakHoursList.innerHTML = `<div class="empty-row">No traffic recorded in this range.</div>`;
    return;
  }

  const max = ranked[0] ? ranked[0].entries : 1;
  elements.peakHoursList.innerHTML = ranked
    .slice(0, 8)
    .map((bucket) => {
      const width = max ? Math.round((bucket.entries / max) * 100) : 0;
      return `
        <article class="path-card peak-hour-card">
          <strong>#${bucket.rank} · ${escapeHtml(formatBucketLabel(bucket.bucket_start))}</strong>
          <div class="progress-bar"><i style="--bar-width: ${width}%"></i></div>
          <small>${formatNumber(bucket.entries)} entries</small>
        </article>
      `;
    })
    .join("");
}

function renderPeriodComparison(response) {
  const deltas = response.deltas || {};
  elements.comparisonPeriodLabel.textContent =
    `${formatDateTime(new Date(response.current.start))} vs ${formatDateTime(new Date(response.previous.start))}`;

  const rows = [
    ["Footfall", "footfall", formatNumber],
    ["Unique Visitors", "unique_visitors", formatNumber],
    ["Queue Joined", "queue_joined", formatNumber],
    ["Queue Completed", "queue_completed", formatNumber],
    ["Queue Abandoned", "queue_abandoned", formatNumber],
    ["Avg Occupancy", "average_occupancy", formatDecimal],
  ];

  const buildCard = (label, data, showDelta) => `
    <article class="comparison-card">
      <header>
        <h3>${label}</h3>
        <span>${formatDateTime(new Date(data.start))} – ${formatDateTime(new Date(data.end))}</span>
      </header>
      <dl>
        ${rows
          .map(
            ([rowLabel, key, formatter]) => `
          <div>
            <dt>${rowLabel}</dt>
            <dd>${formatter(data[key])}${showDelta ? formatDeltaBadge(deltas[key]) : ""}</dd>
          </div>
        `
          )
          .join("")}
      </dl>
    </article>
  `;

  elements.periodComparisonGrid.innerHTML =
    buildCard("Current Period", response.current, true) + buildCard("Previous Period", response.previous, false);
}

function formatDeltaBadge(delta) {
  if (!delta) {
    return "";
  }
  const direction = delta.absolute > 0 ? "up" : delta.absolute < 0 ? "down" : "flat";
  const arrow = direction === "up" ? "▲" : direction === "down" ? "▼" : "—";
  const percentText = delta.percent === null ? "" : ` ${delta.percent > 0 ? "+" : ""}${Math.round(delta.percent * 100)}%`;
  return ` <span class="delta delta-${direction}">${arrow}${percentText}</span>`;
}

// ---------------------------------------------------------------------------
// P4.3: trend-aware alerts. The /anomalies response mixes the pre-existing
// static (all-time) rules with the new trend (comparison-driven) ones --
// both render the same way here; only the "what changed" banner (below)
// singles out trend alerts specifically.
// ---------------------------------------------------------------------------

function renderAlerts(response) {
  const alerts = response.anomalies || [];
  elements.alertsSummary.textContent = alerts.length
    ? `${formatNumber(alerts.length)} ${alerts.length === 1 ? "alert" : "alerts"}`
    : "0 alerts";

  if (!alerts.length) {
    // Honest empty state: absence of a fired rule, not a claim the store is
    // doing well -- see the P4.3 plan's requirement 5.
    elements.alertsList.innerHTML = `<div class="alerts-empty">No significant changes detected for this period.</div>`;
    return;
  }

  // Alerts are already severity-sorted by the API; keep the panel from
  // becoming a wall of cards by only ever showing the top few.
  elements.alertsList.innerHTML = alerts
    .slice(0, MAX_VISIBLE_ALERTS)
    .map((alert) => {
      const relatedMarkup = alert.related_signals && alert.related_signals.length
        ? `<ul class="anomaly-related">${alert.related_signals.map((signal) => `<li>${escapeHtml(signal)}</li>`).join("")}</ul>`
        : "";
      return `
        <article class="anomaly-card severity-${escapeHtml(alert.severity.toLowerCase())}">
          <header>
            <span class="anomaly-severity-label">${escapeHtml(alert.severity)}</span>
            <span class="anomaly-category">${escapeHtml(titleCase(alert.anomaly_type.replace(/_/g, " ")))}</span>
          </header>
          <p>${escapeHtml(alert.message)}</p>
          ${relatedMarkup}
          <span class="anomaly-action">${escapeHtml(alert.suggested_action)}</span>
        </article>
      `;
    })
    .join("");
}

function renderWhatChanged(response) {
  const alerts = response.anomalies || [];
  // Only trend (comparison-driven) alerts are eligible for the headline --
  // the static all-time rules (queue_spike etc) aren't period-over-period
  // findings and don't belong in a "what changed" summary. The list is
  // already severity-sorted, so the first trend match is the strongest one.
  const strongest = alerts.find((alert) => alert.anomaly_type.startsWith("trend_"));

  if (!strongest) {
    elements.whatChangedBanner.hidden = true;
    elements.whatChangedBanner.textContent = "";
    return;
  }

  elements.whatChangedBanner.hidden = false;
  elements.whatChangedBanner.textContent = strongest.message;
}

// ---------------------------------------------------------------------------
// Zone Activity Map (P8) -- shades each configured zone's map outline by a
// real, existing zone-dwell aggregate (visits or dwell time). Deliberately
// zone-level, not a per-point/per-visitor position: see
// docs/DESIGN.md's "Zone-Level Spatial Intelligence, Not Point Projection
// (P8)" section for why. Loaded lazily the first time its tab opens, same
// pattern as Live Analytics above.
// ---------------------------------------------------------------------------

const ZONE_MAP_COLOR_EMPTY = "rgba(150, 140, 120, 0.18)";

function ensureZoneMapLoaded() {
  if (state.zoneMapLoaded) {
    return;
  }
  state.zoneMapLoaded = true;
  loadZoneMap();
}

function refreshZoneMapIfLoaded() {
  if (state.zoneMapLoaded) {
    loadZoneMap();
  }
}

async function loadZoneMap() {
  try {
    const metric = elements.zoneMapMetric.value;
    const response = await fetchJson(`/stores/${state.storeId}/spatial-intensity?metric=${encodeURIComponent(metric)}`);
    renderZoneMap(response);
  } catch (error) {
    setStatus(error.message);
  }
}

function renderZoneMap(response) {
  const zonesWithPolygon = (response.zones || []).filter((zone) => zone.map_polygon && zone.map_polygon.length >= 3);

  if (!response.layout_image_url) {
    showZoneMapEmpty("No store map has been configured for this store yet.");
    return;
  }
  if (!zonesWithPolygon.length) {
    showZoneMapEmpty("This store's map is configured, but no zones have a drawn outline yet.");
    return;
  }

  elements.zoneMapEmpty.hidden = true;
  elements.zoneMapContent.hidden = false;

  const polygonMarkup = zonesWithPolygon
    .map((zone) => {
      const points = zone.map_polygon.map(([x, y]) => `${x},${y}`).join(" ");
      const color = zoneIntensityColor(zone.intensity);
      const label = `${zone.name}: ${formatNumber(zone.visits)} visits, ${formatDuration(zone.average_dwell_seconds)} avg dwell (rank #${zone.rank})`;
      return `<polygon class="zone-map-polygon" points="${points}" fill="${color}"><title>${escapeHtml(label)}</title></polygon>`;
    })
    .join("");

  elements.zoneMapStage.innerHTML = `
    <img src="${escapeHtml(response.layout_image_url)}" alt="${escapeHtml(response.store_id)} store layout">
    <svg class="zone-map-svg" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">${polygonMarkup}</svg>
  `;

  const ranked = [...zonesWithPolygon].sort((a, b) => a.rank - b.rank);
  elements.zoneMapCount.textContent = `${formatNumber(ranked.length)} ${ranked.length === 1 ? "zone" : "zones"}`;
  elements.zoneMapList.innerHTML = ranked
    .map((zone) => {
      const valueText =
        response.metric === "dwell"
          ? `${formatDuration(zone.total_dwell_seconds)} total`
          : `${formatNumber(zone.visits)} visits`;
      return `
        <article class="zone-map-list-item">
          <span class="zone-map-list-swatch" style="background:${zoneIntensityColor(zone.intensity)}"></span>
          <span class="zone-map-list-name">#${zone.rank} ${escapeHtml(zone.name)}</span>
          <span class="zone-map-list-value">${valueText}</span>
        </article>
      `;
    })
    .join("");
}

function showZoneMapEmpty(message) {
  elements.zoneMapEmpty.hidden = false;
  elements.zoneMapEmptyMessage.textContent = message;
  elements.zoneMapContent.hidden = true;
}

function zoneIntensityColor(intensity) {
  if (!intensity) {
    return ZONE_MAP_COLOR_EMPTY;
  }
  const clamped = Math.max(0, Math.min(1, intensity));
  const opacity = 0.22 + clamped * 0.6;
  const hue = 44 - clamped * 34;
  return `hsla(${hue.toFixed(0)}, 74%, 48%, ${opacity.toFixed(2)})`;
}

// ---------------------------------------------------------------------------
// Raw-event replay -- runs synchronously (this project has no background
// task runner) so submitting the form already returns the finished job;
// "Recent Replay Jobs" below is for looking up past runs, not polling.
// ---------------------------------------------------------------------------

const REPLAY_STATUS_LABELS = {
  completed: "Completed",
  partial: "Partial",
  failed: "Failed",
  running: "Running",
};

async function runReplay() {
  elements.replayError.hidden = true;
  elements.replayRunButton.disabled = true;
  elements.replayRunButton.textContent = "Running…";

  const sourceType = elements.replaySourceType.value;
  const body = { source_type: sourceType };
  if (sourceType === "jsonl_file") {
    body.source_ref = elements.replayDataset.value.trim();
  }
  if (elements.replayStart.value) {
    body.range_start = new Date(elements.replayStart.value).toISOString();
  }
  if (elements.replayEnd.value) {
    body.range_end = new Date(elements.replayEnd.value).toISOString();
  }

  try {
    const job = await postJson(`/stores/${state.storeId}/replay`, body);
    renderReplayResult(job);
    await loadReplayJobs();
  } catch (error) {
    elements.replayError.hidden = false;
    elements.replayError.textContent = error.message;
  } finally {
    elements.replayRunButton.disabled = false;
    elements.replayRunButton.textContent = "Run Replay";
  }
}

function renderReplayResult(job) {
  elements.replayResultPanel.hidden = false;
  elements.replayResultStatus.textContent = REPLAY_STATUS_LABELS[job.status] || job.status;
  elements.replayResultKpis.innerHTML = `
    <article class="kpi-card">
      <span>Total Candidates</span>
      <strong>${formatNumber(job.total_events)}</strong>
    </article>
    <article class="kpi-card">
      <span>Accepted</span>
      <strong>${formatNumber(job.accepted_events)}</strong>
      <small>Newly created by this replay</small>
    </article>
    <article class="kpi-card">
      <span>Duplicate</span>
      <strong>${formatNumber(job.duplicate_events)}</strong>
      <small>Already processed -- no change</small>
    </article>
    <article class="kpi-card">
      <span>Failed</span>
      <strong>${formatNumber(job.failed_events)}</strong>
    </article>
  `;

  if (job.error_message) {
    elements.replayResultErrors.innerHTML = `<p class="replay-error-message">${escapeHtml(job.error_message)}</p>`;
  } else if (job.error_details && job.error_details.length) {
    elements.replayResultErrors.innerHTML = job.error_details
      .slice(0, 10)
      .map((detail) => `<p class="replay-error-message">${escapeHtml(detail.item)}: ${escapeHtml(detail.error)}</p>`)
      .join("");
  } else {
    elements.replayResultErrors.innerHTML = "";
  }
}

async function loadReplayJobs() {
  try {
    const response = await fetchJson(`/stores/${state.storeId}/replay`);
    const jobs = response.jobs || [];
    elements.replayJobsCount.textContent = `${formatNumber(jobs.length)} ${jobs.length === 1 ? "job" : "jobs"}`;
    elements.replayJobsBody.innerHTML = jobs.length
      ? jobs
          .map((job) => {
            return `
              <tr>
                <td>${formatDateTime(new Date(job.started_at))}</td>
                <td>${escapeHtml(job.source_type)}${job.source_ref ? ` (${escapeHtml(job.source_ref)})` : ""}</td>
                <td>${escapeHtml(REPLAY_STATUS_LABELS[job.status] || job.status)}</td>
                <td>${formatNumber(job.total_events)}</td>
                <td>${formatNumber(job.accepted_events)}</td>
                <td>${formatNumber(job.duplicate_events)}</td>
                <td>${formatNumber(job.failed_events)}</td>
              </tr>
            `;
          })
          .join("")
      : `<tr><td class="empty-row" colspan="7">No replay jobs yet.</td></tr>`;
  } catch (error) {
    // A viewer without ANALYST access to this store, or no jobs endpoint
    // reachable yet -- show the same table empty state rather than
    // surfacing a raw fetch error on a lazily-loaded tab.
    elements.replayJobsBody.innerHTML = `<tr><td class="empty-row" colspan="7">Unable to load replay jobs.</td></tr>`;
  }
}

function formatBucketLabel(isoString) {
  const date = new Date(isoString);
  if (isNaN(date)) {
    return "—";
  }
  return date.toLocaleString("en-IN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatDateTime(date) {
  if (!(date instanceof Date) || isNaN(date)) {
    return "—";
  }
  return date.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

function renderEmptyState() {
  elements.kpiGrid.innerHTML = "";
  elements.zoneDwellBody.innerHTML = `<tr><td class="empty-row" colspan="4">No data loaded.</td></tr>`;
  elements.funnelTrack.innerHTML = "";
  elements.pathsCount.textContent = "0 journeys";
  elements.commonPaths.innerHTML = "";
  elements.purchasePaths.innerHTML = "";
  elements.dropoffPaths.innerHTML = "";
  elements.insightsList.innerHTML = "";
  elements.insightsCount.textContent = "0 insights";
  elements.comparisonGrid.innerHTML = "";
  elements.heatmapStage.innerHTML = `<div class="heatmap-empty">No heatmap data available.</div>`;
  elements.heatmapSummary.textContent = "0 points";
  elements.revenueLarge.textContent = formatCurrency(0);
  elements.attributionCount.textContent = "0 transactions";
}

function setStatus(message) {
  elements.statusBanner.hidden = !message;
  elements.statusBanner.textContent = message;
}

function formatNumber(value) {
  return new Intl.NumberFormat("en-IN").format(value || 0);
}

function formatCurrency(value) {
  const amount = Math.round(value || 0);
  return `INR ${formatNumber(amount)}`;
}

function formatPercent(value) {
  return `${Math.round((value || 0) * 100)}%`;
}

function formatDecimal(value) {
  return new Intl.NumberFormat("en-IN", {
    maximumFractionDigits: 2,
  }).format(value || 0);
}

function formatDuration(seconds) {
  const rounded = Math.round(seconds || 0);
  if (rounded < 60) {
    return `${rounded}s`;
  }
  const minutes = Math.floor(rounded / 60);
  const remainder = rounded % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

function titleCase(value) {
  return value
    .split(" ")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
