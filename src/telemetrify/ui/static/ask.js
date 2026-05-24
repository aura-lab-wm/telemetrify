// Ask-the-Ledger — Vercel-mode SSE client.
(() => {
  const form    = document.getElementById("ask-form");
  const input   = document.getElementById("ask-input");
  const convo   = document.getElementById("ask-conversation");
  const statusT = document.getElementById("ask-status-text");
  const statusEl = document.getElementById("ask-status");
  const corpusEl = document.getElementById("ask-corpus-size");
  if (!form) return;

  let exchangeNo = 0;

  function setStatus(state, text) {
    statusEl.dataset.state = state;
    statusT.textContent = text;
  }

  fetch("/api/stats").then(r => r.json()).then(d => {
    if (d && d.turns && corpusEl) corpusEl.textContent = d.turns.toLocaleString();
  }).catch(() => {});

  function el(tag, attrs = {}, children = []) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") e.className = v;
      else if (k === "html") e.innerHTML = v;
      else if (v !== null && v !== undefined) e.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
      if (typeof c === "string") e.appendChild(document.createTextNode(c));
      else if (c) e.appendChild(c);
    }
    return e;
  }

  function linkifyCitations(text) {
    return text.replace(/\[#(\d+)\]/g, '<a class="cite-pill" href="/turns/$1">#$1</a>');
  }

  function lightMd(text) {
    // very light markdown for the streamed answer: code, bold, italics, lists, paragraphs
    return text
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^\*]+)\*\*/g, '<strong>$1</strong>')
      .replace(/\*([^\*]+)\*/g, '<em>$1</em>')
      .replace(/^\s*[-*]\s+(.+)$/gm, '<li>$1</li>')
      .replace(/(<li>.*<\/li>\n?)+/g, m => '<ul>' + m + '</ul>')
      .replace(/\n\n+/g, '</p><p>')
      .replace(/^(.+)$/s, '<p>$1</p>');
  }

  function buildExchange(question) {
    exchangeNo += 1;
    const card = el("article", { class: "ask-exchange" });

    const header = el("header", { class: "ask-x-header" }, [
      el("span", { class: "ask-x-num" }, "QUERY № " + String(exchangeNo).padStart(2, "0")),
      el("span", { class: "ask-x-time" }, new Date().toLocaleTimeString("en-GB", { hour12: false })),
      el("span", { class: "ask-x-state pill", "data-state": "planning" }, [
        el("span", { class: "ask-x-state-dot" }), el("span", { class: "ask-x-state-text" }, "planning")
      ]),
    ]);

    const question_el = el("h2", { class: "ask-x-question" }, question);

    const sources_el = el("section", { class: "ask-x-sources", hidden: "" });
    const answer_el  = el("section", { class: "ask-x-answer" }, [
      el("div", { class: "ask-x-answer-body" }, [
        el("div", { class: "ask-skeleton" }, [
          el("span", { class: "ask-skeleton-bar" }),
          el("span", { class: "ask-skeleton-bar w-3-4" }),
          el("span", { class: "ask-skeleton-bar w-5-6" }),
        ])
      ]),
    ]);
    const footer_el = el("footer", { class: "ask-x-footer", hidden: "" });

    card.append(header, question_el, sources_el, answer_el, footer_el);
    if (convo.querySelector(".ask-empty")) convo.querySelector(".ask-empty").remove();
    convo.prepend(card);
    return { card, sources_el, answer_el, footer_el, header };
  }

  function setExchangeState(card, state, text) {
    const stateEl = card.querySelector(".ask-x-state");
    stateEl.dataset.state = state;
    stateEl.querySelector(".ask-x-state-text").textContent = text;
  }

  function renderSources(parentEl, sources) {
    if (!sources || !sources.length) {
      parentEl.hidden = true;
      return;
    }
    parentEl.hidden = false;
    parentEl.innerHTML = "";
    const head = el("div", { class: "ask-src-head" }, [
      el("span", { class: "meta-line" }, `sources · ${sources.length}`),
    ]);
    const list = el("div", { class: "ask-src-grid" });
    sources.forEach((s, idx) => {
      const card = el("a", {
        class: "ask-src-card", href: `/turns/${s.id}`,
        style: `--ix:${idx}`
      });
      card.append(
        el("div", { class: "ask-src-card-head" }, [
          el("span", { class: "ask-src-card-num" }, `#${s.id}`),
          el("span", { class: "ask-src-card-time" }, (s.started_at || "").slice(0, 19).replace("T", " ")),
        ]),
        el("div", { class: "ask-src-card-snippet" }, s.prompt_snippet || ""),
        el("div", { class: "ask-src-card-cwd" }, s.cwd || ""),
      );
      list.appendChild(card);
    });
    parentEl.append(head, list);
  }

  async function ask(question) {
    const x = buildExchange(question);
    setStatus("planning", "planning…");

    try {
      const res = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let answerMd = "";
      let sourcesCount = 0;
      let t0 = performance.now();

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          let evt;
          try { evt = JSON.parse(line.slice(6)); } catch { continue; }
          const body = x.answer_el.querySelector(".ask-x-answer-body");
          if (evt.event === "plan") {
            setExchangeState(x.card, "retrieving", `retrieving · k=${evt.data.k}`);
            setStatus("retrieving", `retrieving · k=${evt.data.k}`);
          }
          else if (evt.event === "sources") {
            sourcesCount = evt.data.length;
            renderSources(x.sources_el, evt.data);
            setExchangeState(x.card, "synthesizing", `synthesizing · ${sourcesCount} sources`);
            setStatus("synthesizing", `synthesizing · ${sourcesCount} sources`);
          }
          else if (evt.event === "delta") {
            answerMd += evt.data;
            body.innerHTML = linkifyCitations(lightMd(answerMd));
          }
          else if (evt.event === "done") {
            const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
            setExchangeState(x.card, "done", "done");
            setStatus("ready", "ready");
            const cost = evt.data && evt.data.cost_usd_today;
            x.footer_el.hidden = false;
            x.footer_el.innerHTML = `
              <span class="meta-line">complete</span>
              <span class="ask-x-stat">${elapsed}s elapsed</span>
              <span class="ask-x-stat">${sourcesCount} cited</span>
              ${typeof cost === "number" ? `<span class="ask-x-stat">$${cost.toFixed(4)} spent today</span>` : ""}
            `;
          }
          else if (evt.event === "error") {
            body.innerHTML = `<div class="ask-x-error">${evt.data}</div>`;
            setExchangeState(x.card, "error", "error");
            setStatus("error", "error");
          }
        }
      }
    } catch (e) {
      x.answer_el.querySelector(".ask-x-answer-body").innerHTML =
        `<div class="ask-x-error">${e.message}</div>`;
      setStatus("error", "error");
      setExchangeState(x.card, "error", "error");
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    ask(q);
  });

  document.querySelectorAll(".ask-suggest").forEach(b => {
    b.addEventListener("click", () => {
      const q = b.dataset.q;
      if (q) { input.value = q; ask(q); input.value = ""; }
    });
  });

  // ⌘K / Ctrl-K focus the input
  document.addEventListener("keydown", (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      input.focus();
      input.select();
    }
  });
})();
