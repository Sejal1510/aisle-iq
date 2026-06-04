const state = {
  storeId: "ST1001",
  comparisonStores: ["ST1001", "ST1002"],
  metrics: null,
  funnel: null,
  heatmap: null,
  insights: null,
  paths: null,
};

const elements = {
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
};

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
  state.storeId = elements.storeInput.value.trim().toUpperCase() || "ST1001";
  state.comparisonStores = Array.from(new Set([state.storeId, "ST1002"]));
  loadDashboard();
});

loadDashboard();

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
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`Unable to load ${path}: ${response.status}`);
  }
  return response.json();
}

function activateView(viewName) {
  const title = viewName === "comparison" ? "Store Comparison" : titleCase(viewName);
  elements.pageTitle.textContent = title;
  elements.tabButtons.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.view === viewName);
  });
  elements.viewPanels.forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.viewPanel === viewName);
  });
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
