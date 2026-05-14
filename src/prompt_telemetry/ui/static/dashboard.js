// Loads each /api/charts/<name> endpoint and renders into the matching div.
// Also paints the system-health tile.

const CHARTS = [
  ["turns_per_day",   "chart-turns-per-day"],
  ["tokens_by_model", "chart-tokens-by-model"],
  ["tool_heatmap",    "chart-tool-heatmap"],
  ["error_rate",      "chart-error-rate"],
  ["latency",         "chart-latency"],
  ["annotations",     "chart-annotations"],
  ["correction_rate", "chart-correction-rate"],
  ["top_clusters",    "chart-top-clusters"],
];

const CONFIG = { responsive: true, displaylogo: false };

async function loadChart(name, divId) {
  const el = document.getElementById(divId);
  if (!el) return;
  try {
    const res = await fetch(`/api/charts/${name}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const fig = await res.json();
    // Force dark theme defaults even if the server forgot.
    fig.layout = Object.assign({ template: "plotly_dark" }, fig.layout || {});
    fig.layout.paper_bgcolor = "#161a22";
    fig.layout.plot_bgcolor = "#161a22";
    fig.layout.font = Object.assign({ color: "#e6e8ec" }, fig.layout.font || {});
    await Plotly.newPlot(el, fig.data || [], fig.layout, CONFIG);
  } catch (err) {
    el.innerHTML = `<div class="chart-error">failed to load ${name}: ${err.message}</div>`;
  }
}

async function loadHealth() {
  const body = document.getElementById("health-body");
  const tile = document.getElementById("health-tile");
  if (!body || !tile) return;
  try {
    const res = await fetch("/api/health");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const h = await res.json();
    tile.classList.remove("status-ok", "status-degraded", "status-empty");
    tile.classList.add(`status-${h.status}`);
    body.innerHTML = `
      <span class="health-status">${h.status}</span>
      <span><b>${h.sessions}</b> sessions</span>
      <span><b>${h.turns}</b> turns</span>
      <span><b>${h.tool_calls}</b> tool calls</span>
      <span><b>${h.annotations}</b> annotations</span>
      <span>vec coverage <b>${(h.vec_coverage * 100).toFixed(1)}%</b></span>
      <span>last turn <code>${(h.last_turn_at || "—").slice(0,19)}</code></span>
    `;
  } catch (err) {
    body.textContent = `health check failed: ${err.message}`;
  }
}

function init() {
  loadHealth();
  CHARTS.forEach(([name, id]) => loadChart(name, id));
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
