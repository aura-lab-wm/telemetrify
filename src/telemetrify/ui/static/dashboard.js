// The Telemetry Ledger — dashboard wiring.
// Populates the masthead/narrative-fold from /api/dashboard/headline, then
// renders twelve Plotly figures from /api/charts/<name>. The server already
// returns Ledger-themed layouts (paper/plot bg, font, colorway, grid),
// so this file does not override visual layout — it only newPlot's.
//
// Ordered to match the on-page group order (see dashboard.html):
//   i.   Trust & evidence      — unsupported_claim_rate, command_outcome_rate
//   ii.  Reliability           — tool_heatmap, error_rate, latency
//   iii. Prompt clusters       — top_clusters, cluster_correction_breakdown
//   iv.  Activity/correction/cost — turns_per_day, tokens_by_model,
//        correction_rate, annotations, cache_efficiency

const CHARTS = [
  ["unsupported_claim_rate",       "chart-unsupported-claim-rate"],
  ["command_outcome_rate",         "chart-command-outcome-rate"],

  ["tool_heatmap",                 "chart-tool-heatmap"],
  ["error_rate",                   "chart-error-rate"],
  ["latency",                      "chart-latency"],

  ["top_clusters",                 "chart-top-clusters"],
  ["cluster_correction_breakdown", "chart-cluster-correction-breakdown"],

  ["turns_per_day",                "chart-turns-per-day"],
  ["tokens_by_model",              "chart-tokens-by-model"],
  ["correction_rate",              "chart-correction-rate"],
  ["annotations",                  "chart-annotations"],
  ["cache_efficiency",             "chart-cache-efficiency"],
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
// Delegates to window.TM.formatTimeAgo (loaded from utils.js in base.html).
function formatTimeAgo(iso) {
  return (window.TM && window.TM.formatTimeAgo) ? window.TM.formatTimeAgo(iso) : null;
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

// Plotly does not auto-wrap or auto-truncate SVG title text — on a narrow
// plate (a phone-width single column, or a tablet two-up column) a title
// like "Fig. 10 · cluster correction breakdown (top 10 by size)" simply
// gets clipped mid-word with no ellipsis, which is the opposite of "wrap/
// truncate gracefully". Below a per-plate width threshold, greedily word-
// wrap the title across up to three lines sized to the container's actual
// width (rather than a single fixed split point, which isn't enough for the
// longer titles) and trim the font size slightly. Wide plates are left
// completely untouched — this only ever fires when the container is
// genuinely too narrow for the title as authored.
const NARROW_TITLE_BREAKPOINT = 560;
const TITLE_FONT_SIZE_NARROW = 12;
// Average glyph width for the Geist/system-ui title font at 12px — used to
// estimate how many characters fit per line for a given pixel width.
// Deliberately generous (0.68, not the ~0.55-0.58 "average English prose"
// rule of thumb): measured against real rendered titles, an optimistic
// factor let a 39-character title ("Fig. 11 · command outcome rate
// (weekly)") slide through the "already fits" check at a 284px container
// and get silently clipped by Plotly's SVG anyway. Wrapping a title that
// would have just barely fit is a trivial cosmetic cost; failing to wrap
// one that doesn't fit is the actual bug this function exists to prevent —
// so this errs deliberately toward wrapping too eagerly, not too rarely.
const APPROX_CHAR_WIDTH = TITLE_FONT_SIZE_NARROW * 0.68;
const TITLE_MAX_LINES = 3;

function greedyWrap(text, maxCharsPerLine, maxLines) {
  const words = text.split(" ");
  const lines = [];
  let current = "";
  for (const word of words) {
    const candidate = current ? `${current} ${word}` : word;
    if (current && candidate.length > maxCharsPerLine && lines.length < maxLines - 1) {
      lines.push(current);
      current = word;
    } else {
      current = candidate;
    }
  }
  if (current) lines.push(current);
  return lines.join("<br>");
}

function adaptTitleForWidth(fig, containerWidth) {
  if (!fig || !fig.layout || !(containerWidth > 0) || containerWidth >= NARROW_TITLE_BREAKPOINT) {
    return fig;
  }
  const title = fig.layout.title;
  const text = typeof title === "string" ? title : title && title.text;
  if (!text) return fig;

  const maxCharsPerLine = Math.max(12, Math.floor(containerWidth / APPROX_CHAR_WIDTH));
  if (text.length <= maxCharsPerLine) return fig; // already fits on one line

  const wrapped = greedyWrap(text, maxCharsPerLine, TITLE_MAX_LINES);
  const lineCount = wrapped.split("<br>").length;
  const prevFont = (typeof title === "object" && title.font) || {};
  fig.layout.title = {
    ...(typeof title === "object" ? title : {}),
    text: wrapped,
    font: { ...prevFont, size: TITLE_FONT_SIZE_NARROW },
  };
  fig.layout.margin = {
    ...(fig.layout.margin || {}),
    t: Math.max((fig.layout.margin || {}).t || 40, 30 + lineCount * 15),
  };
  return fig;
}

// Long categorical axis labels (tool/command names, cluster labels like
// "start-in-chrome__read_page") hit the same clip-with-no-ellipsis problem
// as titles once a plate is squeezed onto a phone-width single column.
// Plotly's own automargin grows the axis margin to fit long tick labels,
// but only up to the space the container actually has — below that it
// still clips, so on narrow containers also shrink the tick font a touch
// and make sure automargin is on, maximising how much label survives.
function adaptAxesForWidth(fig, containerWidth) {
  if (!fig || !fig.layout || !(containerWidth > 0) || containerWidth >= NARROW_TITLE_BREAKPOINT) {
    return fig;
  }
  for (const key of Object.keys(fig.layout)) {
    if (!/^[xy]axis/.test(key)) continue;
    const axis = fig.layout[key] || {};
    const prevTickFont = axis.tickfont || {};
    fig.layout[key] = {
      ...axis,
      automargin: true,
      tickfont: { ...prevTickFont, size: Math.min(prevTickFont.size || 11, 10) },
    };
  }
  return fig;
}

// Server-side truncation (_shorten_tool_name in charts.py) picks a single
// generous default length for a y-axis category label like a full MCP
// tool name ("mcp__servername__toolname") — generous enough for most
// widths, but automargin can only grow the left margin so far before it
// starts eating into the plate's own plot area; on a genuinely narrow
// (phone-width, ~320px) plate the server's default is still too long and
// the label clips again, this time against the page/container edge
// rather than mid-word. Re-truncate further, client-side, sized to the
// container's REAL measured width, keeping the same "keep the tail,
// prefix an ellipsis" rule the server already applies — the distinguishing
// part of an MCP tool name is at the end.
const CATEGORY_LABEL_WIDTH_FRACTION = 0.62; // how much of the plate width category labels may claim
const CATEGORY_LABEL_CHAR_PX = 6;           // ~glyph width for Geist Mono at the 10px narrow tickfont
const CATEGORY_LABEL_SAFETY_PX = 14;        // measured margin for tick padding/gap Plotly adds beyond the raw text width

function adaptCategoryLabelsForWidth(fig, containerWidth) {
  if (!fig || !Array.isArray(fig.data) || !(containerWidth > 0) || containerWidth >= NARROW_TITLE_BREAKPOINT) {
    return fig;
  }
  const maxChars = Math.max(
    6,
    Math.floor((containerWidth * CATEGORY_LABEL_WIDTH_FRACTION - CATEGORY_LABEL_SAFETY_PX) / CATEGORY_LABEL_CHAR_PX)
  );
  for (const trace of fig.data) {
    if (trace.type !== "heatmap" || !Array.isArray(trace.y)) continue;
    trace.y = trace.y.map((label) => {
      if (typeof label !== "string" || label.length <= maxChars) return label;
      const keep = Math.max(3, maxChars - 1);
      return "…" + label.slice(-keep);
    });
  }
  return fig;
}

// ── Legend vs. axis-title collision (see charts.py _LEDGER_BASE comment) ──
// The server ships a fixed-pixel bottom margin plus a fixed-FRACTION
// legend.y, intended to keep the horizontal legend clear of the x-axis
// title. That combination only works at one plot-area height: Plotly
// positions legend.y as a fraction of the plot area (container height
// minus margins), and our chart containers range from ~230px tall on a
// phone up to ~500px on a 3440px+ display (see the `.chart`/`.chart-tall`
// clamp() in app.css) — so a fixed fraction of a height that varies more
// than 2x produces a varying ABSOLUTE pixel gap, and at most of that range
// the gap is too small: the legend visibly overlaps the axis title and/or
// rotated tick labels. This reproduced across nearly the entire tested
// width range (320px–2560px), not just new fluid breakpoints, because the
// root cause (fraction-of-a-shrinking-area) was never actually
// width/breakpoint-dependent.
//
// Fix: recompute `margin.b` and `legend.y` from the chart div's ACTUAL
// measured clientHeight (and clientWidth, to estimate legend row count)
// every time we render — initial paint AND every later resize (see
// observeResize below) — so the pixel clearance between the axis line and
// the legend's top edge is a small constant, regardless of container
// height.
const AXIS_TITLE_RESERVE_PX = 46; // tick labels + standoff + axis-title text
const LEGEND_GAP_PX = 8;          // breathing room between title and legend
const LEGEND_ROW_PX = 20;         // one legend row at 11px font
const LEGEND_MAX_ROWS = 4;        // generous cap on rows we plan margin for
const MIN_PLOT_AREA_PX = 90;      // never squeeze the plot itself away to nothing

function estimateLegendRows(fig, containerWidth) {
  const legend = fig.layout && fig.layout.legend;
  if (!legend || legend.orientation !== "h" || !(containerWidth > 0)) return 1;
  const entries = Array.isArray(fig.data)
    ? fig.data.filter((t) => t && t.showlegend !== false && t.name)
    : [];
  if (entries.length <= 1) return 1;
  const longestName = entries.reduce((m, t) => Math.max(m, String(t.name).length), 0);
  // swatch + inter-item gap + text at ~11px/glyph — approximate, tuned
  // against the actual legends on this dashboard (2-4 short items, and
  // tokens-by-model's much longer model-name items).
  const itemWidthPx = 34 + longestName * 6.4;
  const perRow = Math.max(1, Math.floor(containerWidth / itemWidthPx));
  return Math.min(LEGEND_MAX_ROWS, Math.max(1, Math.ceil(entries.length / perRow)));
}

// Position the legend using `yref: "container"` (a fraction of the WHOLE
// chart div, top=1/bottom=0) rather than the default `yref: "paper"` (a
// fraction of the plot area, i.e. the div minus margins). This isn't
// cosmetic: with the default "paper" ref, the more we grow `margin.b` to
// make room for a tall legend, the SMALLER the plot area gets — and since
// `y` is a fraction of THAT shrinking area, reaching the same absolute
// pixel target requires an ever-larger-magnitude `y`. Empirically, once
// that magnitude passed roughly ±2, Plotly silently reset `y` back to
// ~-0.02 (essentially ignoring our request) rather than honouring it —
// which reopened the exact axis-title collision this whole adaptation
// exists to prevent. Container-relative `y` doesn't have that feedback
// loop: the denominator (the div's own height) never shrinks just because
// margin.b grows, so the values involved stay small and Plotly renders
// them as asked at every container size we've tested.
function legendYForBand(marginB, containerHeight) {
  return (marginB - (AXIS_TITLE_RESERVE_PX + LEGEND_GAP_PX)) / containerHeight;
}

function adaptLegendForHeight(fig, containerWidth, containerHeight) {
  if (!fig || !fig.layout || !fig.layout.legend || !(containerHeight > 0)) return fig;
  const legend = fig.layout.legend;
  if (legend.orientation !== "h") return fig; // this collision is specific to below-plot horizontal legends

  const marginT = (fig.layout.margin && fig.layout.margin.t) || 56;
  const estRows = estimateLegendRows(fig, containerWidth);
  // IMPORTANT: a horizontal legend that wraps across MULTIPLE rows stops
  // respecting a manually set `y`/`yanchor` in this Plotly version — once
  // it wraps, Plotly silently recomputes its own vertical position
  // instead of honouring ours, which reopens exactly the title collision
  // this function exists to prevent (confirmed empirically: forcing a
  // wrapped 4-item legend to a specific y rendered it back at y≈-0.02,
  // practically inside the plot area). A VERTICAL legend has no such
  // override — Plotly honours `y`/`yanchor` for it exactly like a
  // single-row horizontal one. So once more than one row would be
  // needed, switch orientation to "v" (one item per row, unconditionally,
  // so the row count is exact rather than an estimate) instead of trying
  // to fight Plotly's internal wrapped-legend positioning.
  const entries = Array.isArray(fig.data)
    ? fig.data.filter((t) => t && t.showlegend !== false && t.name)
    : [];
  const useVertical = estRows > 1;
  const rows = useVertical ? Math.max(1, entries.length) : estRows;

  const wantedBandPx = AXIS_TITLE_RESERVE_PX + LEGEND_GAP_PX + rows * LEGEND_ROW_PX;
  // Cap how much of the container the bottom margin can claim so the plot
  // area never collapses to nothing on the shortest (phone) containers.
  const marginB = Math.max(60, Math.min(wantedBandPx, containerHeight - marginT - MIN_PLOT_AREA_PX));

  fig.layout.margin = { ...(fig.layout.margin || {}), b: marginB };
  fig.layout.legend = {
    ...legend,
    orientation: useVertical ? "v" : "h",
    yanchor: "top",
    yref: "container",
    y: legendYForBand(marginB, containerHeight),
  };
  return fig;
}

// adaptLegendForHeight's row estimate (and the entries.length row count it
// uses once it switches a legend to vertical) is still only an estimate of
// exactly how tall Plotly renders the legend — font metrics/kerning aren't
// something we can compute from JS without a real measurement. When the
// granted `margin.b` band turns out a little too small for the legend
// Plotly actually draws, Plotly slides it up to keep it fully on-canvas
// rather than letting it spill past the container's bottom edge — which
// defeats the whole purpose here, since "up" is straight into the
// axis-title band we were protecting. Rather than trying to perfectly
// predict the rendered height in advance, MEASURE what actually got drawn
// and, if it landed higher than intended, recompute the margin/position
// from the real measured legend height (not a guess) and re-render once.
// This is what makes the fix correct regardless of how many legend
// entries a chart ends up with (e.g. tokens-by-model's model list).
function measureLegendBox(el) {
  const legendEl = el.querySelector(".legend");
  if (!legendEl) return null;
  const elRect = el.getBoundingClientRect();
  const legendRect = legendEl.getBoundingClientRect();
  return { top: legendRect.top - elRect.top, height: legendRect.height };
}

const RECONCILE_MAX_PASSES = 14;
// Plotly.react on this version, for a dual-y-axis chart (a fixed-range
// yaxis2 overlaying yaxis — command_outcome_rate, cache_efficiency),
// throws "Something went wrong with axis scaling" once the resulting
// PLOT AREA gets too short — empirically or, on a 230px-tall container,
// ~40px of plot area renders fine and ~30px throws deterministically
// every time, regardless of how the margin got there (a single big jump
// and a sequence of small steps both fail at the same absolute
// threshold). RECONCILE_MIN_PLOT_AREA_PX keeps every correction this
// function makes on the safe side of that cliff. RECONCILE_MAX_STEP_PX
// still caps how much margin.b moves per react() call — it doesn't avoid
// the cliff by itself, but it keeps any OTHER Plotly relayout hiccup
// (e.g. automargin still settling) from compounding into a big single
// jump.
const RECONCILE_MAX_STEP_PX = 20;
const RECONCILE_MIN_PLOT_AREA_PX = 45;

async function reconcileLegendPlacement(el, working) {
  if (!working.layout || !working.layout.legend) return;
  const containerHeight = el.clientHeight;
  const marginT = (working.layout.margin && working.layout.margin.t) || 56;

  for (let pass = 0; pass < RECONCILE_MAX_PASSES; pass++) {
    const box = measureLegendBox(el);
    if (!box) return;
    const marginB = (working.layout.margin && working.layout.margin.b) || 0;
    const intendedTopPx = containerHeight - marginB + AXIS_TITLE_RESERVE_PX + LEGEND_GAP_PX;
    const overlapsTitle = box.top < intendedTopPx - 2;
    // A legend with many rows (e.g. tokens-by-model, which can have up to
    // _MAX_MODEL_SERIES+1 entries) can also simply be TALLER than the
    // margin.b band we granted, even when its top landed exactly where we
    // asked — in which case it spills past the container's own bottom
    // edge into whatever is below it on the page, a different but equally
    // real visual bug. Check for both failure modes.
    const overflowsBottom = box.top + box.height > containerHeight + 2;

    // Plotly drew the legend where we asked, fully within bounds — the
    // estimate was good enough, nothing left to correct.
    if (!overlapsTitle && !overflowsBottom) return;

    // Recompute using the ACTUAL measured legend height instead of a
    // guess. Avoiding the axis-title collision (and keeping the legend
    // from spilling into whatever follows the chart) is the explicit
    // priority here, so — unlike adaptLegendForHeight's initial, more
    // conservative estimate — this correction is allowed to shrink the
    // plot area further than that. It is NOT allowed below
    // RECONCILE_MIN_PLOT_AREA_PX, though: past that point Plotly itself
    // starts throwing on a dual-y-axis chart (see above), so on the most
    // extreme containers (narrowest phone width + a many-row legend) a
    // few px of legend may still spill past the container's own bottom
    // edge — a much smaller, lower-priority residual than the axis-title
    // collision this function's main job is to prevent.
    const neededBandPx = AXIS_TITLE_RESERVE_PX + LEGEND_GAP_PX + box.height + 4;
    const targetMarginB = Math.min(
      neededBandPx,
      Math.max(RECONCILE_MIN_PLOT_AREA_PX, containerHeight - marginT - RECONCILE_MIN_PLOT_AREA_PX)
    );
    const delta = targetMarginB - marginB;

    // Retry with a progressively SMALLER step before giving up on this
    // pass — the "axis scaling" failure is more likely the bigger the
    // single jump is, so a step that's still too big gets a couple of
    // chances at a smaller one rather than abandoning the correction
    // after one failure (this matters most right after a resize that
    // changed the container by a lot in one go, e.g. 1600px → 280px).
    let stepPx = RECONCILE_MAX_STEP_PX;
    let applied = false;
    for (let attempt = 0; attempt < 3 && !applied; attempt++) {
      const newMarginB = Math.abs(delta) <= stepPx ? targetMarginB : marginB + Math.sign(delta) * stepPx;
      const candidateMargin = { ...working.layout.margin, b: newMarginB };
      const candidateLegend = {
        ...working.layout.legend,
        yanchor: "top",
        yref: "container",
        y: legendYForBand(newMarginB, containerHeight),
      };
      try {
        await Plotly.react(
          el, working.data,
          { ...working.layout, margin: candidateMargin, legend: candidateLegend },
          PLOT_CONFIG
        );
        working.layout.margin = candidateMargin;
        working.layout.legend = candidateLegend;
        applied = true;
      } catch (err) {
        stepPx = Math.max(2, Math.floor(stepPx / 2));
      }
    }
    if (!applied) {
      // Repeated failures even at a small step — stop reconciling rather
      // than leave an unhandled rejection. Whatever rendered on the last
      // successful pass stands, and the next resize/render pass gets
      // another chance to converge.
      return;
    }
  }
}

// Apply `working`'s layout to `el`, but never in a single `margin.b` jump
// bigger than RECONCILE_MAX_STEP_PX when the div already has a PREVIOUS
// plot rendered (e.g. a resize/fold/devtools-emulation re-render that
// lands on a very differently-sized container than the last one) — same
// Plotly.react "axis scaling" crash risk as reconcileLegendPlacement, just
// on the first call of a render pass instead of a later correction pass.
// A div's very first-ever plot has no prior state to jump FROM, so it's
// never at risk and goes straight through.
async function reactToLayoutSafely(el, working) {
  const hasPrevPlot = Array.isArray(el.data) && el.data.length > 0 && el.layout && el.layout.margin;
  const prevMarginB = hasPrevPlot ? el.layout.margin.b : null;
  const targetMarginB = (working.layout.margin && working.layout.margin.b) || 0;
  if (prevMarginB === null || Math.abs(targetMarginB - prevMarginB) <= RECONCILE_MAX_STEP_PX) {
    return Plotly.react(el, working.data, working.layout || {}, PLOT_CONFIG);
  }
  const containerHeight = el.clientHeight;
  const steps = Math.ceil(Math.abs(targetMarginB - prevMarginB) / RECONCILE_MAX_STEP_PX);
  let result;
  for (let i = 1; i <= steps; i++) {
    const stepMarginB = prevMarginB + (targetMarginB - prevMarginB) * (i / steps);
    const stepLayout = { ...working.layout, margin: { ...working.layout.margin, b: stepMarginB } };
    if (working.layout.legend && working.layout.legend.yref === "container") {
      stepLayout.legend = { ...working.layout.legend, y: legendYForBand(stepMarginB, containerHeight) };
    }
    try {
      result = await Plotly.react(el, working.data, stepLayout, PLOT_CONFIG);
    } catch (err) {
      // As in reconcileLegendPlacement: a rare Plotly.react internal
      // failure here (most likely if the container itself keeps resizing
      // mid-step) shouldn't surface as an unhandled rejection. Stop
      // stepping and keep whatever rendered on the last successful pass —
      // the next resize/render pass gets another chance to reach the
      // exact target.
      break;
    }
  }
  return result;
}

// ── Per-chart render state, so every re-render (resize, fold, devtools
// device emulation) starts from the PRISTINE server payload rather than
// compounding adaptations on top of an already-wrapped title or an
// already-shrunk margin. ─────────────────────────────────────────────────
const chartState = new Map(); // divId -> { el, fig }

async function renderAdapted(divId) {
  const state = chartState.get(divId);
  if (!state) return;
  const { el, fig } = state;
  const working = JSON.parse(JSON.stringify(fig));
  // The server already painted the layout in Ledger tones — don't override
  // colors/grid/paper. Title/axis wrapping, category-label truncation, and
  // legend/margin placement are the responsive exceptions (see the adapt*
  // functions above), and all are re-derived from the container's CURRENT
  // measured size on every render, not just the first one.
  adaptTitleForWidth(working, el.clientWidth);
  adaptAxesForWidth(working, el.clientWidth);
  adaptCategoryLabelsForWidth(working, el.clientWidth);
  adaptLegendForHeight(working, el.clientWidth, el.clientHeight);
  await reactToLayoutSafely(el, working);
  await reconcileLegendPlacement(el, working);
}

// Re-adapt on resize — a plain window-resize handler is not enough: a
// foldable device changing fold state, or a devtools/emulator viewport
// override, resizes the chart's CONTAINER without necessarily firing a
// page navigation, and our title-wrap/axis/legend adaptations previously
// only ever ran once, at the initial render. That staleness is exactly
// what let a title wrapped (or left unwrapped) for one container width
// survive, unchanged, into a later render at a much narrower or wider
// width — producing clipped/overflowing titles that "shouldn't" have been
// possible per the wrap logic itself. ResizeObserver watches the actual
// chart div's box, so it fires for any cause of a size change, not just a
// window resize.
const RESIZE_DEBOUNCE_MS = 120;

function observeResize(el, divId) {
  let lastW = el.clientWidth;
  let lastH = el.clientHeight;
  let timer = null;
  const ro = new ResizeObserver(() => {
    const w = el.clientWidth;
    const h = el.clientHeight;
    if (Math.abs(w - lastW) < 2 && Math.abs(h - lastH) < 2) return; // ignore sub-pixel churn
    lastW = w;
    lastH = h;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => { renderAdapted(divId); }, RESIZE_DEBOUNCE_MS);
  });
  ro.observe(el);
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
    chartState.set(divId, { el, fig });
    await renderAdapted(divId);
    if (typeof ResizeObserver !== "undefined") observeResize(el, divId);
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
