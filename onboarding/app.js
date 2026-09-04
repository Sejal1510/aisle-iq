// AisleIQ P7 onboarding UI. Deliberately minimal, plain JS, no build step or
// framework -- this is an admin/configuration tool, not the polished
// analytics dashboard (see docs/CHOICES.md's P7 entry). It talks only to the
// P7 onboarding API (app/api/onboarding.py) and reuses the existing
// JWT/StoreAccess auth (/auth/login) -- no separate auth system.
const TOKEN_KEY = "aisleiq_onboarding_token";

const state = {
  token: sessionStorage.getItem(TOKEN_KEY),
  storeId: null,
  zones: [],
  cameras: [],
  coverageByCamera: {},
  activeMap: null,
  canvasImage: null,
  drawMode: null, // null | "zone" | "coverage"
  drawPoints: [],
};

const el = (id) => document.getElementById(id);

function showLoginGate() {
  el("login-gate").hidden = false;
  el("app-shell").hidden = true;
}

function showAppShell() {
  el("login-gate").hidden = true;
  el("app-shell").hidden = false;
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  if (options.json !== undefined) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ? JSON.stringify(body.detail) : detail;
    } catch (_err) {
      /* non-JSON error body */
    }
    throw new Error(`${response.status}: ${detail}`);
  }
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return response.json();
  return response;
}

// ---------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------
el("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  el("login-error").hidden = true;
  try {
    const body = await api("/auth/login", {
      method: "POST",
      json: { email: el("login-email").value.trim(), password: el("login-password").value },
    });
    state.token = body.access_token;
    sessionStorage.setItem(TOKEN_KEY, state.token);
    showAppShell();
  } catch (err) {
    el("login-error").hidden = false;
    el("login-error").textContent = "Sign-in failed: " + err.message;
  }
});

el("logout-button").addEventListener("click", () => {
  state.token = null;
  sessionStorage.removeItem(TOKEN_KEY);
  showLoginGate();
});

// ---------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------
el("load-store-button").addEventListener("click", () => loadStore(el("store-id-input").value.trim()));

el("create-store-button").addEventListener("click", async () => {
  const storeId = el("new-store-id").value.trim();
  if (!storeId) return;
  try {
    await api("/stores", { method: "POST", json: { store_id: storeId, name: el("new-store-name").value.trim() || null } });
    el("store-id-input").value = storeId;
    await loadStore(storeId);
  } catch (err) {
    setStoreStatus("Create failed: " + err.message, true);
  }
});

function setStoreStatus(text, isError) {
  const node = el("store-status");
  node.textContent = text;
  node.className = isError ? "error" : "muted";
}

async function loadStore(storeId) {
  if (!storeId) return;
  state.storeId = storeId;
  setStoreStatus(`Loading '${storeId}'...`, false);
  try {
    const [zones, cameras, maps] = await Promise.all([
      api(`/stores/${storeId}/config/zones`),
      api(`/stores/${storeId}/config/cameras`),
      api(`/stores/${storeId}/config/maps`),
    ]);
    state.zones = zones;
    state.cameras = cameras;
    state.activeMap = maps.find((m) => m.is_active) || null;
    renderZonesTable();
    renderCamerasTable();
    populateCoverageCameraSelect();
    populateCoverageZoneSelect();
    if (state.activeMap) await loadCanvasImage(state.activeMap.file_url);
    setStoreStatus(`Loaded '${storeId}': ${zones.length} zone(s), ${cameras.length} camera(s).`, false);
  } catch (err) {
    setStoreStatus("Load failed: " + err.message, true);
  }
}

// ---------------------------------------------------------------------
// Map + canvas
// ---------------------------------------------------------------------
const canvas = el("drawing-canvas");
const ctx = canvas.getContext("2d");

el("upload-map-button").addEventListener("click", async () => {
  const file = el("map-file-input").files[0];
  if (!file || !state.storeId) return;
  const formData = new FormData();
  formData.append("file", file);
  const name = el("map-name-input").value.trim();
  if (name) formData.append("name", name);
  const map = await api(`/stores/${state.storeId}/config/maps`, { method: "POST", body: formData });
  state.activeMap = map;
  await loadCanvasImage(map.file_url);
});

function loadCanvasImage(url) {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      state.canvasImage = img;
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      redrawCanvas();
      resolve();
    };
    img.onerror = reject;
    img.src = url;
  });
}

function redrawCanvas() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (state.canvasImage) ctx.drawImage(state.canvasImage, 0, 0, canvas.width, canvas.height);
  if (state.drawPoints.length) drawPolygonDraft();
}

function drawPolygonDraft() {
  ctx.strokeStyle = "#c55345";
  ctx.fillStyle = "#c55345";
  ctx.lineWidth = 2;
  ctx.beginPath();
  state.drawPoints.forEach(([nx, ny], index) => {
    const x = nx * canvas.width;
    const y = ny * canvas.height;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
    ctx.fillRect(x - 3, y - 3, 6, 6);
  });
  ctx.stroke();
}

canvas.addEventListener("click", (event) => {
  if (!state.drawMode) return;
  const rect = canvas.getBoundingClientRect();
  const nx = (event.clientX - rect.left) / rect.width;
  const ny = (event.clientY - rect.top) / rect.height;
  state.drawPoints.push([round4(nx), round4(ny)]);
  redrawCanvas();
});

function round4(value) {
  return Math.round(value * 10000) / 10000;
}

function startDrawing(mode, hintText) {
  state.drawMode = mode;
  state.drawPoints = [];
  el("draw-hint").textContent = hintText;
  el("draw-controls").hidden = false;
  redrawCanvas();
}

el("cancel-draw-button").addEventListener("click", () => {
  state.drawMode = null;
  state.drawPoints = [];
  el("draw-hint").textContent = "";
  el("draw-controls").hidden = true;
  redrawCanvas();
});

let pendingPolygonTarget = null; // "zone" | "coverage"

el("draw-zone-shape-button").addEventListener("click", () => {
  if (!state.activeMap) {
    alert("Upload a store map first.");
    return;
  }
  pendingPolygonTarget = "zone";
  startDrawing("zone", "Click at least 3 points on the map outlining this zone, then Finish.");
});

el("draw-coverage-shape-button").addEventListener("click", async () => {
  const camera = state.cameras.find((c) => c.camera_id === el("coverage-camera-select").value);
  if (!camera) return;
  const backdrop = camera.reference_image_url || (state.activeMap ? state.activeMap.file_url : null);
  if (!backdrop) {
    alert("Upload a reference image for this camera, or upload a store map, before drawing.");
    return;
  }
  await loadCanvasImage(backdrop);
  pendingPolygonTarget = "coverage";
  startDrawing("coverage", "Click at least 3 points on this camera's frame outlining the zone it covers, then Finish.");
});

el("finish-polygon-button").addEventListener("click", () => {
  if (state.drawPoints.length < 3) {
    alert("A polygon needs at least 3 points.");
    return;
  }
  if (pendingPolygonTarget === "zone") {
    el("zone-shape-status").textContent = `${state.drawPoints.length}-point shape captured.`;
  } else if (pendingPolygonTarget === "coverage") {
    el("coverage-shape-status").textContent = `${state.drawPoints.length}-point shape captured.`;
  }
  state.drawMode = null;
  el("draw-hint").textContent = "";
  el("draw-controls").hidden = true;
});

// ---------------------------------------------------------------------
// Zones
// ---------------------------------------------------------------------
function renderZonesTable() {
  const body = el("zones-table-body");
  body.innerHTML = "";
  state.zones.forEach((zone) => {
    const row = document.createElement("tr");
    row.innerHTML = `<td>${zone.zone_id}</td><td>${zone.name}</td><td>${zone.zone_type}</td>` +
      `<td>${zone.is_revenue_zone ? "yes" : "no"}</td><td>${zone.map_polygon ? zone.map_polygon.length + " pts" : "-"}</td>`;
    body.appendChild(row);
  });
}

el("create-zone-button").addEventListener("click", async () => {
  if (!state.storeId) return alert("Load or create a store first.");
  const zoneId = el("zone-id-input").value.trim();
  const name = el("zone-name-input").value.trim();
  if (!zoneId || !name) return alert("Zone ID and name are required.");

  const payload = {
    zone_id: zoneId,
    name,
    zone_type: el("zone-type-input").value,
    is_revenue_zone: el("zone-revenue-input").checked,
  };
  if (pendingPolygonTarget === "zone" && state.drawPoints.length >= 3) {
    payload.map_polygon = state.drawPoints;
  }

  try {
    await api(`/stores/${state.storeId}/config/zones`, { method: "POST", json: payload });
    el("zone-id-input").value = "";
    el("zone-name-input").value = "";
    el("zone-shape-status").textContent = "";
    state.drawPoints = [];
    pendingPolygonTarget = null;
    await loadStore(state.storeId);
  } catch (err) {
    alert("Could not create zone: " + err.message);
  }
});

// ---------------------------------------------------------------------
// Cameras
// ---------------------------------------------------------------------
function renderCamerasTable() {
  const body = el("cameras-table-body");
  body.innerHTML = "";
  state.cameras.forEach((camera) => {
    const row = document.createElement("tr");
    row.innerHTML = `<td>${camera.camera_id}</td><td>${camera.name || "-"}</td><td>${camera.role || "-"}</td>` +
      `<td>${camera.video_path || "-"}</td><td>${camera.reference_image_url ? "uploaded" : "-"}</td>`;
    body.appendChild(row);
  });
}

el("create-camera-button").addEventListener("click", async () => {
  if (!state.storeId) return alert("Load or create a store first.");
  const cameraId = el("camera-id-input").value.trim();
  const role = el("camera-role-input").value;
  if (!cameraId) return alert("Camera ID is required.");

  const payload = {
    camera_id: cameraId,
    name: el("camera-name-input").value.trim() || null,
    role,
    video_path: el("camera-video-path-input").value.trim() || null,
    start_time: el("camera-start-time-input").value ? new Date(el("camera-start-time-input").value).toISOString() : null,
    sample_fps: numberOrNull(el("camera-sample-fps-input").value),
    confidence_threshold: numberOrNull(el("camera-confidence-input").value),
    queue_completion_seconds: numberOrNull(el("camera-queue-complete-input").value),
    queue_abandonment_seconds: numberOrNull(el("camera-queue-abandon-input").value),
  };

  try {
    await api(`/stores/${state.storeId}/config/cameras`, { method: "POST", json: payload });
    el("camera-id-input").value = "";
    el("camera-name-input").value = "";
    el("camera-video-path-input").value = "";
    await loadStore(state.storeId);
  } catch (err) {
    alert("Could not create camera: " + err.message);
  }
});

function numberOrNull(value) {
  if (value === "" || value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isNaN(parsed) ? null : parsed;
}

// ---------------------------------------------------------------------
// Coverage
// ---------------------------------------------------------------------
function populateCoverageCameraSelect() {
  const select = el("coverage-camera-select");
  select.innerHTML = "";
  state.cameras.forEach((camera) => {
    const option = document.createElement("option");
    option.value = camera.camera_id;
    option.textContent = `${camera.camera_id} (${camera.role || "?"})`;
    select.appendChild(option);
  });
  onCoverageCameraChange();
}

function populateCoverageZoneSelect() {
  const select = el("coverage-zone-select");
  select.innerHTML = "";
  state.zones.forEach((zone) => {
    const option = document.createElement("option");
    option.value = zone.zone_id;
    option.textContent = `${zone.zone_id} (${zone.name})`;
    select.appendChild(option);
  });
}

el("coverage-camera-select").addEventListener("change", onCoverageCameraChange);

async function onCoverageCameraChange() {
  const cameraId = el("coverage-camera-select").value;
  const camera = state.cameras.find((c) => c.camera_id === cameraId);
  el("coverage-entry-controls").hidden = !camera || camera.role !== "entry";
  el("coverage-zone-controls").hidden = !camera || camera.role === "entry";
  if (camera) await renderCoverageTable(cameraId);
}

el("entry-position-input").addEventListener("input", () => {
  el("entry-position-value").textContent = Number(el("entry-position-input").value).toFixed(2);
  redrawCanvas();
  drawEntryLinePreview();
});

function drawEntryLinePreview() {
  if (!state.canvasImage) return;
  const axis = el("entry-axis-select").value;
  const position = Number(el("entry-position-input").value);
  ctx.strokeStyle = "#2f6f9f";
  ctx.lineWidth = 3;
  ctx.beginPath();
  if (axis === "x") {
    const x = position * canvas.width;
    ctx.moveTo(x, 0);
    ctx.lineTo(x, canvas.height);
  } else {
    const y = position * canvas.height;
    ctx.moveTo(0, y);
    ctx.lineTo(canvas.width, y);
  }
  ctx.stroke();
}

el("upload-reference-button").addEventListener("click", async () => {
  const cameraId = el("coverage-camera-select").value;
  const file = el("camera-reference-file-input").files[0];
  if (!cameraId || !file) return;
  const formData = new FormData();
  formData.append("file", file);
  const camera = await api(`/stores/${state.storeId}/config/cameras/${cameraId}/reference-image`, {
    method: "POST",
    body: formData,
  });
  const index = state.cameras.findIndex((c) => c.camera_id === cameraId);
  if (index >= 0) state.cameras[index] = camera;
  renderCamerasTable();
});

el("save-entry-coverage-button").addEventListener("click", async () => {
  const cameraId = el("coverage-camera-select").value;
  if (!cameraId) return;
  const payload = {
    zone_id: null,
    geometry: {
      kind: "line",
      axis: el("entry-axis-select").value,
      position: Number(el("entry-position-input").value),
      inside_greater_than_position: el("entry-inside-greater-input").checked,
    },
  };
  try {
    await api(`/stores/${state.storeId}/config/cameras/${cameraId}/coverage`, { method: "POST", json: payload });
    await renderCoverageTable(cameraId);
  } catch (err) {
    alert("Could not save entry line: " + err.message);
  }
});

el("save-coverage-button").addEventListener("click", async () => {
  const cameraId = el("coverage-camera-select").value;
  const zoneId = el("coverage-zone-select").value;
  if (!cameraId || !zoneId) return;
  if (pendingPolygonTarget !== "coverage" || state.drawPoints.length < 3) {
    alert("Draw a shape on the camera frame first.");
    return;
  }
  const payload = { zone_id: zoneId, geometry: { kind: "polygon", points: state.drawPoints } };
  try {
    await api(`/stores/${state.storeId}/config/cameras/${cameraId}/coverage`, { method: "POST", json: payload });
    state.drawPoints = [];
    pendingPolygonTarget = null;
    el("coverage-shape-status").textContent = "";
    await renderCoverageTable(cameraId);
  } catch (err) {
    alert("Could not save coverage: " + err.message);
  }
});

async function renderCoverageTable(cameraId) {
  const rows = await api(`/stores/${state.storeId}/config/cameras/${cameraId}/coverage`);
  state.coverageByCamera[cameraId] = rows;
  const body = el("coverage-table-body");
  body.innerHTML = "";
  rows.forEach((coverage) => {
    const row = document.createElement("tr");
    const shape = coverage.geometry.kind === "polygon"
      ? `${coverage.geometry.points.length}-point polygon`
      : `line (${coverage.geometry.axis}=${coverage.geometry.position})`;
    row.innerHTML = `<td>${coverage.coverage_id.slice(0, 8)}</td><td>${coverage.zone_id || "-"}</td><td>${shape}</td><td></td>`;
    const deleteButton = document.createElement("button");
    deleteButton.textContent = "Delete";
    deleteButton.addEventListener("click", async () => {
      await api(`/stores/${state.storeId}/config/cameras/${cameraId}/coverage/${coverage.coverage_id}`, { method: "DELETE" });
      await renderCoverageTable(cameraId);
    });
    row.lastElementChild.appendChild(deleteButton);
    body.appendChild(row);
  });
}

// ---------------------------------------------------------------------
// Startup
// ---------------------------------------------------------------------
if (state.token) showAppShell();
else showLoginGate();
