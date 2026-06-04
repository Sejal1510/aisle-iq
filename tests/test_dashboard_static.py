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
    assert 'id="insights-list"' in html
    assert 'id="common-paths"' in html
    assert 'id="heatmap-stage"' in html
    assert "Store Heatmap" in html
    assert "Total Visitors" in script
    assert "Staff Filtered" in script
    assert "Avg Group Size" in script
    assert "Store Comparison" in html


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
