/* pulse.js — polls /api/pulse every 30s, updates the topbar chip
   everywhere and the home Pulse card when present. */
(function () {
  const POLL_MS = 30_000;

  const fmt = (window.TM && window.TM.formatTimeAgo) || (() => "—");
  const compact = (window.TM && window.TM.formatCompact) || ((n) => String(n));
  const withCommas = (window.TM && window.TM.formatWithCommas) || ((n) => String(n));

  let lastTurnAtSeen = null;

  function setChip(data) {
    const chip = document.querySelector(".topbar-pulse");
    if (!chip) return;
    if (!data || data.last_turn_at === null) {
      chip.dataset.state = "idle";
      chip.querySelector(".topbar-pulse__label").textContent = "no captures yet";
      return;
    }
    const live = !!data.is_live;
    chip.dataset.state = live ? "live" : "idle";
    const ago = fmt(data.last_turn_at, true) || "—";
    const rate = data.last_hour ? `${data.last_hour.turns}/hr` : "";
    chip.querySelector(".topbar-pulse__label").textContent =
      live ? `live · ${ago} · ${rate}`
           : `idle · ${ago}`;
  }

  // Pulse the brand mark on the masthead whenever a new turn lands.
  function flashBrandIfNew(data) {
    if (!data || !data.last_turn_at) return;
    if (lastTurnAtSeen === null) { lastTurnAtSeen = data.last_turn_at; return; }
    if (data.last_turn_at !== lastTurnAtSeen) {
      lastTurnAtSeen = data.last_turn_at;
      const mark = document.querySelector(".brand-mark");
      if (!mark) return;
      mark.classList.remove("brand-mark--ping");
      // force reflow so re-adding the class re-runs the animation
      void mark.offsetWidth;
      mark.classList.add("brand-mark--ping");
    }
  }

  function setCard(data) {
    const card = document.querySelector(".pulse-card");
    if (!card || !data) return;
    card.dataset.state = data.is_live ? "live" : "idle";

    const ago = card.querySelector(".pulse-card__ago");
    if (ago) ago.textContent = data.last_turn_at
      ? fmt(data.last_turn_at) : "no captures yet";

    const hr = data.last_hour || {};
    const set = (sel, val) => {
      const el = card.querySelector(sel);
      if (el) el.textContent = val;
    };
    set('[data-pulse="hr-turns"]',  withCommas(hr.turns || 0));
    set('[data-pulse="hr-tokens"]', compact(hr.tokens || 0));
    set('[data-pulse="hr-errors"]', String(hr.errors || 0));
    set('[data-pulse="hr-model"]',  hr.top_model || "—");
    set('[data-pulse="hr-cwd"]',    hr.top_cwd_basename
      ? `~/…/${hr.top_cwd_basename}` : "—");

    const d24 = data.last_24h || {};
    set('[data-pulse="d24-turns"]',  withCommas(d24.turns || 0));
    set('[data-pulse="d24-tokens"]', compact(d24.tokens || 0));
    set('[data-pulse="d24-errors"]', String(d24.errors || 0));

    // sparkline
    if (Array.isArray(d24.sparkline)) renderSpark(card, d24.sparkline);

    // recent turns
    const list = card.querySelector('[data-pulse="recent"]');
    if (list && Array.isArray(data.recent_turns)) {
      list.innerHTML = data.recent_turns.map(r => {
        const ago = fmt(r.started_at, true) || "";
        const snip = (r.snippet || "").replace(/[<>&]/g, c =>
          ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]));
        const model = (r.model || "").replace(/^claude-/, "");
        return `<li class="pulse-recent__row">
          <a href="/turns/${r.id}" class="pulse-recent__id">#${r.id}</a>
          <span class="pulse-recent__ago">${ago}</span>
          <span class="pulse-recent__model">${model}</span>
          <span class="pulse-recent__snip">${snip}</span>
        </li>`;
      }).join("");
    }
  }

  function renderSpark(card, buckets) {
    const svg = card.querySelector(".pulse-spark");
    if (!svg) return;
    const W = 220, H = 28, P = 2;
    const n = buckets.length;
    const max = Math.max(1, ...buckets);
    const colW = (W - P * 2) / n;
    let bars = "";
    for (let i = 0; i < n; i++) {
      const h = Math.max(1, Math.round(((H - P * 2) * buckets[i]) / max));
      const x = P + i * colW;
      const y = H - P - h;
      const cur = (i === n - 1);
      bars += `<rect x="${x.toFixed(2)}" y="${y}" width="${(colW * 0.78).toFixed(2)}" height="${h}" rx="0.5"
        fill="${cur ? 'var(--phosphor)' : 'var(--ink-faint)'}"></rect>`;
    }
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = bars;
  }

  async function tick() {
    if (document.visibilityState !== "visible") return;
    try {
      const r = await fetch("/api/pulse", { cache: "no-store" });
      if (!r.ok) return;
      const data = await r.json();
      setChip(data);
      setCard(data);
      flashBrandIfNew(data);
    } catch (_) { /* swallow — polling resumes on next interval */ }
  }

  function start() {
    tick();
    setInterval(tick, POLL_MS);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") tick();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
