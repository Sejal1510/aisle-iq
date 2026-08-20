# PROMPT: Generate static dashboard tests that verify required dashboard panels and FastAPI route registration.
# CHANGES MADE: Kept checks lightweight so they validate integration without depending on a browser runtime.
from pathlib import Path

from starlette.routing import Mount

from app.main import app

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = PROJECT_ROOT / "dashboard"


def test_dashboard_assets_exist() -> None:
    assert (DASHBOARD_DIR / "index.html").is_file()
    assert (DASHBOARD_DIR / "styles.css").is_file()
    assert (DASHBOARD_DIR / "app.js").is_file()


def test_dashboard_is_mounted() -> None:
    dashboard_mounts = [
        route for route in app.routes if isinstance(route, Mount) and route.path == "/dashboard"
    ]

    assert len(dashboard_mounts) == 1


def test_dashboard_contains_required_views() -> None:
    html = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert 'data-view="overview"' in html
    assert 'data-view="funnel"' in html
    assert 'data-view="paths"' in html
    assert 'data-view="insights"' in html
    assert 'data-view="comparison"' in html
    assert 'data-view="live-analytics"' in html
    assert 'id="insights-list"' in html
    assert 'id="common-paths"' in html
    assert 'id="heatmap-stage"' in html
    assert "Store Heatmap" in html
    assert "Total Visitors" in script
    assert "Staff Filtered" in script
    assert "Avg Group Size" in script
    assert "Store Comparison" in html
    assert "Live Analytics" in html


def test_dashboard_consumes_existing_analytics_apis() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "fetchJson(`/stores/${state.storeId}/metrics`)" in script
    assert "fetchJson(`/stores/${state.storeId}/funnel`)" in script
    assert "fetchJson(`/stores/${state.storeId}/heatmap`)" in script
    assert "fetchJson(`/stores/${state.storeId}/insights`)" in script
    assert "fetchJson(`/stores/${state.storeId}/paths`)" in script
    assert "fetchJson(`/stores/${storeId}/metrics`)" in script


def test_dashboard_renders_insights_panel_from_api_contract() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")
    styles = (DASHBOARD_DIR / "styles.css").read_text(encoding="utf-8")

    assert "function renderInsights(response)" in script
    assert "insight.severity" in script
    assert "insight.explanation" in script
    assert "insight.recommendation" in script
    assert ".insight-card" in styles
    assert ".severity-high" in styles


def test_dashboard_renders_path_panel_from_api_contract() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")
    styles = (DASHBOARD_DIR / "styles.css").read_text(encoding="utf-8")

    assert "function renderPaths(response)" in script
    assert "response.most_common_paths" in script
    assert "response.top_purchase_journeys" in script
    assert "response.path_drop_offs" in script
    assert ".path-card" in styles


def test_dashboard_renders_heatmap_overlay_from_api_contract() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")
    styles = (DASHBOARD_DIR / "styles.css").read_text(encoding="utf-8")

    assert "function renderHeatmap(heatmap)" in script
    assert "heatmap.layout_image_url" in script
    assert "heatmap.points" in script
    assert "--x:" in script
    assert "--y:" in script
    assert "--diameter:" in script
    assert "--opacity:" in script
    assert ".heatmap-layout" in styles
    assert ".heatmap-layer" in styles
    assert ".heatmap-point" in styles


# ---------------------------------------------------------------------------
# P4.2: Live Analytics tab -- integrates all 8 P3 endpoints into the dashboard.
# ---------------------------------------------------------------------------


def test_live_analytics_view_and_controls_exist_in_html() -> None:
    html = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="live-analytics-view"' in html
    assert 'data-view-panel="live-analytics"' in html
    assert 'id="analytics-as-of"' in html
    assert 'id="range-preset"' in html
    assert 'id="range-custom"' in html
    assert 'id="range-start"' in html
    assert 'id="range-end"' in html
    assert 'id="range-apply"' in html
    assert 'id="range-error"' in html
    assert 'id="analytics-empty"' in html
    assert 'id="analytics-content"' in html
    assert 'id="analytics-kpi-grid"' in html


def test_live_analytics_consumes_all_8_p3_endpoints() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "fetchJson(`/stores/${state.storeId}/occupancy/current`)" in script
    assert "fetchJson(`/stores/${state.storeId}/occupancy/history?${query}`)" in script
    assert "fetchJson(`/stores/${state.storeId}/queue/current`)" in script
    assert "fetchJson(`/stores/${state.storeId}/queue/metrics?${query}`)" in script
    assert "fetchJson(`/stores/${state.storeId}/footfall/hourly?${query}`)" in script
    assert "fetchJson(`/stores/${state.storeId}/queue/hourly?${query}`)" in script
    assert "fetchJson(`/stores/${state.storeId}/peak-hours?${query}`)" in script
    assert "fetchJson(`/stores/${state.storeId}/comparison?${query}`)" in script


def test_live_analytics_derives_range_from_health_endpoint_not_hardcoded() -> None:
    """F-P4.2: the default range must come from GET /health's per-store
    last_event_timestamp, not from a wall-clock assumption or a literal
    demo date/store baked into the dashboard logic."""
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert 'fetchJson("/health")' in script
    assert "last_event_timestamp" in script
    assert "2026-06" not in script  # no hardcoded P4.1 demo date
    assert "new Date()" not in script  # no wall-clock "now" fallback


def test_live_analytics_is_loaded_lazily_on_first_tab_open() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "function ensureLiveAnalyticsLoaded()" in script
    assert "liveAnalyticsLoaded" in script
    # The only trigger into Live Analytics loading on tab activation is the
    # guarded ensureLiveAnalyticsLoaded() call, and it fires from exactly one
    # place: inside activateView (a click handler), not at script load time.
    assert script.count("ensureLiveAnalyticsLoaded();") == 1
    activate_view_body = script.split("function activateView(viewName) {")[1].split("\n}\n")[0]
    assert "ensureLiveAnalyticsLoaded();" in activate_view_body
    # Initial page load calls only the existing loadDashboard() -- not
    # loadLiveAnalytics -- so opening the dashboard without visiting the
    # Live Analytics tab fires none of its 8 requests. Checked as an
    # unindented (column-0) statement so this doesn't false-positive on the
    # indented calls inside the range-control event handler callbacks.
    assert "\nloadDashboard();\n" in script
    assert "\nloadLiveAnalytics();\n" not in script
    assert "\nensureLiveAnalyticsLoaded();\n" not in script


def test_live_analytics_refreshes_on_store_change_and_range_change() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "function refreshLiveAnalyticsIfLoaded()" in script
    assert "refreshLiveAnalyticsIfLoaded();" in script
    assert 'elements.rangePreset.addEventListener("change"' in script
    assert 'elements.rangeApply.addEventListener("click"' in script


def test_live_analytics_render_functions_exist() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    for function_name in [
        "renderAnalyticsKpis",
        "renderOccupancyHistory",
        "renderHourlyFootfall",
        "renderQueueActivityChart",
        "renderPeakHours",
        "renderPeriodComparison",
    ]:
        assert f"function {function_name}(" in script


def test_live_analytics_empty_states_are_honest_not_fabricated() -> None:
    """F-P4.2: zero occupancy/queue are real values (never suppressed); a
    missing average wait must not render as "0s"; a missing peak hour must
    not fabricate a winner; abandonment rate must not render as 0% when
    there were no queue visits at all to compute it from."""
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "average_wait_seconds === null" in script
    assert "hasQueueVolume" in script
    assert "response.peak_hour" in script
    assert "No traffic recorded in this range." in script


def test_live_analytics_validates_custom_range_client_side() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "function isValidRange(start, end)" in script
    assert "end > start" in script
    assert "isValidRange(start, end)" in script


def test_live_analytics_as_of_wording_is_present_not_realtime() -> None:
    """Required refinement 1: honest 'as of <timestamp>' wording near the
    range controls, no streaming/real-time claim."""
    html = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "analytics-as-of" in html
    assert "As of ${formatDateTime(asOf)}" in script
    assert "WebSocket" not in script
    assert "setInterval" not in script


def test_live_analytics_css_components_exist() -> None:
    styles = (DASHBOARD_DIR / "styles.css").read_text(encoding="utf-8")

    assert ".kpi-grid--analytics" in styles
    assert ".bar-chart" in styles
    assert ".bar-chart-bar--joined" in styles
    assert ".bar-chart-bar--completed" in styles
    assert ".bar-chart-bar--abandoned" in styles
    assert ".analytics-empty" in styles
    assert ".range-controls" in styles
    assert ".delta-up" in styles
    assert ".delta-down" in styles


def test_existing_tabs_and_endpoints_are_unchanged() -> None:
    """P4.2 must not touch the 5 pre-existing tabs/endpoints -- regression
    guard alongside test_dashboard_consumes_existing_analytics_apis."""
    html = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")

    for view in ["overview", "funnel", "paths", "insights", "comparison"]:
        assert f'data-view="{view}"' in html
    assert 'id="comparison-grid"' in html  # store-vs-store, untouched
    assert 'id="period-comparison-grid"' in html  # new, distinct id -- no collision
