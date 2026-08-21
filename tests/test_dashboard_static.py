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
    # P4.4: initial page load is gated behind auth -- showAppShell() (called
    # once the user is authenticated, either from a stored token or right
    # after login) calls the existing loadDashboard(), not loadLiveAnalytics,
    # so opening the dashboard without visiting the Live Analytics tab still
    # fires none of its 8 requests.
    show_app_shell_body = script.split("function showAppShell() {")[1].split("\n}\n")[0]
    assert "loadDashboard();" in show_app_shell_body
    assert "loadLiveAnalytics();" not in show_app_shell_body
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


# ---------------------------------------------------------------------------
# P4.3: trend-aware alerts panel + "what changed" banner in Live Analytics.
# ---------------------------------------------------------------------------


def test_p43_alerts_panel_and_banner_exist_in_html() -> None:
    html = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="what-changed-banner"' in html
    assert 'id="alerts-list"' in html
    assert 'id="alerts-summary"' in html
    # Must live inside the same #analytics-content wrapper the 8 P3 panels
    # already use, so the tab-level empty state hides it too, not just the
    # existing panels.
    content_section = html.split('id="analytics-content"')[1].split("</section>\n      </main>")[0]
    assert "what-changed-banner" in content_section
    assert "alerts-list" in content_section


def test_p43_anomalies_call_uses_the_shared_range_and_is_not_eager() -> None:
    """The anomalies fetch must ride the same lazy-loading/range machinery as
    the 8 P3 calls -- not a separate eager fetch, and not fired at module
    load time (see the P4.2 lazy-loading tests for the general pattern)."""
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "fetchJson(`/stores/${state.storeId}/anomalies?${query}`)" in script
    # Inside the same Promise.all as the 8 P3 calls, not a second fetch call.
    load_body = script.split("async function loadLiveAnalytics()")[1].split("\n}\n")[0]
    assert "anomalies?${query}" in load_body


def test_p43_render_functions_exist() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "function renderAlerts(response)" in script
    assert "function renderWhatChanged(response)" in script
    assert "renderAlerts(anomalies)" in script
    assert "renderWhatChanged(anomalies)" in script


def test_p43_alerts_empty_state_is_honest_not_a_performance_claim() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "No significant changes detected for this period." in script
    # Must not claim the store is doing well -- only that no rule fired.
    assert "performing well" not in script.lower()
    assert "everything looks good" not in script.lower()


def test_p43_alerts_panel_caps_visible_count() -> None:
    """Requirement 5: the panel must not become a wall of cards."""
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "MAX_VISIBLE_ALERTS" in script
    assert ".slice(0, MAX_VISIBLE_ALERTS)" in script


def test_p43_related_signals_render_separately_from_the_primary_message() -> None:
    """Requirement 1/4: an observed change and its related/contributing
    signals must be visually distinguishable, not concatenated into one
    unstructured sentence."""
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert "alert.related_signals" in script
    assert "anomaly-related" in script


def test_p43_severity_css_uses_anomaly_vocabulary_not_insight_vocabulary() -> None:
    """AnomalyService's severity vocabulary (INFO/WARN/CRITICAL) is different
    from the Insights tab's (HIGH/MEDIUM/LOW) -- must not be lossily mapped
    onto the wrong CSS classes."""
    styles = (DASHBOARD_DIR / "styles.css").read_text(encoding="utf-8")

    assert ".anomaly-card.severity-info" in styles
    assert ".anomaly-card.severity-warn" in styles
    assert ".anomaly-card.severity-critical" in styles


def test_p43_does_not_expand_the_8_p3_endpoint_calls() -> None:
    """Requirement 5 of the original P4.2 approval and requirement 5 of this
    task: the 8 P3 endpoints stay exactly 8 -- anomalies is a 9th, separate,
    pre-existing endpoint being extended, not a new P3 endpoint."""
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    for endpoint in [
        "occupancy/current",
        "occupancy/history",
        "queue/current",
        "queue/metrics",
        "footfall/hourly",
        "queue/hourly",
        "peak-hours",
        "comparison",
    ]:
        assert script.count(f"/{endpoint}") >= 1


def test_p44_hidden_attribute_actually_hides_login_gate_and_app_shell() -> None:
    """Regression guard: .login-gate/.app-shell's own `display: grid` is an
    author-stylesheet rule, which beats the browser's built-in
    `[hidden] { display: none }` rule regardless of specificity (author
    origin always wins over user-agent origin) -- without an explicit
    [hidden] override, toggling the `hidden` property in JS silently does
    nothing and both panels render stacked at once."""
    styles = (DASHBOARD_DIR / "styles.css").read_text(encoding="utf-8")

    assert ".app-shell[hidden]" in styles
    assert ".login-gate[hidden]" in styles
    assert "display: none;" in styles.split(".app-shell[hidden]")[1][:200]


def test_p44_login_gate_exists_in_html() -> None:
    html = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8")

    assert 'id="login-gate"' in html
    assert 'id="login-form"' in html
    assert 'id="login-email"' in html
    assert 'id="login-password"' in html
    assert 'id="app-shell"' in html
    # The app shell must start hidden -- the login gate is the default view,
    # not something layered on top of an already-visible dashboard.
    assert '<div class="app-shell" id="app-shell" hidden>' in html


def test_p44_fetchjson_attaches_bearer_token_and_handles_401() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    fetch_json_body = script.split("async function fetchJson(path) {")[1].split("\n}\n")[0]
    assert "state.authToken" in fetch_json_body
    assert "Authorization" in fetch_json_body
    assert "Bearer" in fetch_json_body
    assert "401" in fetch_json_body
    assert "showLoginGate" in fetch_json_body


def test_p44_login_form_submits_credentials_and_stores_token() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert 'elements.loginForm.addEventListener("submit"' in script
    assert '"/auth/login"' in script
    # sessionStorage, not localStorage -- a token should not outlive the
    # browser tab/session it was issued in.
    assert "sessionStorage.setItem(AUTH_TOKEN_STORAGE_KEY" in script


def test_p44_logout_clears_token_and_shows_login_gate() -> None:
    script = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")

    assert 'elements.logoutButton.addEventListener("click"' in script
    show_login_gate_body = script.split("function showLoginGate() {")[1].split("\n}\n")[0]
    assert "sessionStorage.removeItem(AUTH_TOKEN_STORAGE_KEY)" in show_login_gate_body
    assert "state.authToken = null" in show_login_gate_body
