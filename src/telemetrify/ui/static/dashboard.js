// The Telemetry Ledger — dashboard wiring.
// Populates the masthead/narrative-fold from /api/dashboard/headline, then
// renders ten Plotly figures from /api/charts/<name>. The server already
// returns Ledger-themed layouts (paper/plot bg, font, colorway, grid),
// so this file does not override visual layout — it only newPlot's.

const CHARTS = [
  ["turns_per_day",                "chart-turns-per-day"],
  ["tokens_by_model",              "chart-tokens-by-model"],
  ["tool_heatmap",                 "chart-tool-heatmap"],
  ["error_rate",                   "chart-error-rate"],
  ["latency",                      "chart-latency"],
  ["annotations",                  "chart-annotations"],
  ["correction_rate",              "chart-correction-rate"],
  ["top_clusters",                 "chart-top-clusters"],
  ["cache_efficiency",             "chart-cache-efficiency"],
  ["cluster_correction_breakdown", "chart-cluster-correction-breakdown"],
];

const PLOT_CONFIG = { responsive: true, displaylogo: false };

/* ── Formatting helpers ─────────────────────────────────────────────── */

// "16 March 2026" — long-form English date in en-GB order.
function formatLongDate(iso) {
  if (!iso) return null;
  // SQLite timestamps come back like "2026-03-16 14:22:01" (no T, no Z).
  // Treating as UTC keeps server-vs-client consistent.
  const normalised = iso.includes("T") ? iso : iso.replace(" ", "T") + "Z";
  const d = new Date(normalised);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "long",
    year: "numeric",
  });
}

// "3 minutes ago" / "2 hours ago" / "yesterday" / "{N} days ago"
function formatTimeAgo(iso) {
  if (!iso) return null;
  const normalised = iso.includes("T") ? iso : iso.replace(" ", "T") + "Z";
  const then = new Date(normalised);
  if (Number.isNaN(then.getTime())) return null;
  const diffMs = Date.now() - then.getTime();
  const minutes = Math.max(0, Math.round(diffMs / 60000));
  if (minutes < 1)  return "just now";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24)   return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days  = Math.round(hours / 24);
  if (days === 1)   return "yesterday";
  return `${days} days ago`;
}

// Compact number formatting: 7300 → "7,300", 1_200_000 → "1.2M".
// For the fold "tokens" pill specifically — tight, glanceable.
function formatCompact(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1_000_000) {
    const v = n / 1_000_000;
    return `${v >= 10 ? v.toFixed(1).replace(/\.0$/, "") : v.toFixed(2).replace(/\.?0+$/, "")}M`;
  }
  if (abs >= 10_000) {
    return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}K`;
  }
  return n.toLocaleString("en-US");
}

function formatWithCommas(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toLocaleString("en-US");
}

/* ── Masthead + fold ────────────────────────────────────────────────── */

function setMastheadDate() {
  const el = document.getElementById("masthead-date");
  if (!el) return;
  el.textContent = new Date().toLocaleDateString("en-GB", {
    day: "numeric",
    month: "long",
    year: "numeric",
  });
}

function renderNarrative(h) {
  const el = document.getElementById("masthead-narrative");
  if (!el) return;
  const parts = [];

  const sessions = formatWithCommas(h.sessions);
  const turns    = formatWithCommas(h.turns);
  parts.push(`${sessions} session${h.sessions === 1 ? "" : "s"}`);

  const firstDate = formatLongDate(h.first_at);
  if (firstDate) {
    parts.push(`${turns} turns since ${firstDate}`);
  } else {
    parts.push(`${turns} turns recorded`);
  }

  const ago = formatTimeAgo(h.last_at);
  if (ago) parts.push(`last entry ${ago}`);

  if (h.cache_hit_pct !== undefined && h.cache_hit_pct !== null) {
    parts.push(`${Math.round(h.cache_hit_pct)}% cache hit ratio`);
  }

  if (h.top_model && h.top_model.model) {
    parts.push(`top model ${h.top_model.model}`);
  }

  el.textContent = parts.join(" · ") + ".";
}

function renderFold(h) {
  const row = document.getElementById("fold-row");
  if (!row) return;

  const items = [
    { label: "sessions",      value: formatWithCommas(h.sessions) },
    { label: "turns",         value: formatWithCommas(h.turns) },
    { label: "tokens",        value: formatCompact(h.tokens) },
    { label: "follow-up rate", value: `${(h.followup_pct ?? 0).toFixed(1)}%` },
    { label: "clusters",      value: formatWithCommas(h.clusters) },
    { label: "annotations",   value: formatWithCommas(h.annotations) },
  ];

  row.innerHTML = items.map(({ label, value }) => `
    <span class="fold-item">
      <span class="fold-label">${label}</span>
      <span class="fold-value">${value}</span>
    </span>
  `).join("");
}

function renderFooter(h) {
  const el = document.getElementById("footer-sessions");
  if (!el) return;
  el.textContent = formatWithCommas(h.sessions);
}

async function loadHeadline() {
  try {
    const res = await fetch("/api/dashboard/headline");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const h = await res.json();
    renderNarrative(h);
    renderFold(h);
    renderFooter(h);
  } catch (err) {
    const sub = document.getElementById("masthead-narrative");
    if (sub) sub.innerHTML = `<span class="muted">(headline unavailable)</span>`;
    const row = document.getElementById("fold-row");
    if (row) row.innerHTML = "";
  }
}

/* ── Charts ─────────────────────────────────────────────────────────── */

function renderError(el, name) {
  if (!el) return;
  el.innerHTML = `<div class="chart-error">Plate ${name} failed to render.</div>`;
}

async function loadChart(name, divId) {
  const el = document.getElementById(divId);
  if (!el) return;
  try {
    const res = await fetch(`/api/charts/${name}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const fig = await res.json();
    if (!fig || !Array.isArray(fig.data) || fig.data.length === 0) {
      // empty payload — surface gently, do not throw
      renderError(el, name);
      return;
    }
    // The server already painted the layout in Ledger tones — don't override.
    await Plotly.newPlot(el, fig.data, fig.layout || {}, PLOT_CONFIG);
  } catch (err) {
    renderError(el, name);
  }
}

/* ── Init ───────────────────────────────────────────────────────────── */

async function init() {
  setMastheadDate();
  // headline + every chart in parallel — page paints progressively
  const tasks = [loadHeadline(), ...CHARTS.map(([n, id]) => loadChart(n, id))];
  await Promise.all(tasks);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
