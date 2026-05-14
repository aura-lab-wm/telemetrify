// Ask-the-Ledger SSE client.
(() => {
  const form = document.getElementById("ask-form");
  const input = document.getElementById("ask-input");
  const convo = document.getElementById("ask-conversation");
  const status = document.getElementById("ask-status");
  if (!form) return;

  function el(tag, attrs = {}, children = []) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") e.className = v;
      else if (k === "html") e.innerHTML = v;
      else e.setAttribute(k, v);
    }
    for (const c of [].concat(children)) {
      if (typeof c === "string") e.appendChild(document.createTextNode(c));
      else if (c) e.appendChild(c);
    }
    return e;
  }

  function linkifyCitations(text) {
    return text.replace(/\[#(\d+)\]/g, '<a class="cite" href="/turns/$1">#$1</a>');
  }

  function appendExchange(question) {
    if (convo.querySelector(".ask-empty")) convo.querySelector(".ask-empty").remove();
    const card = el("div", { class: "ask-exchange" });
    const q = el("div", { class: "ask-question" }, [el("span", { class: "ask-q-label" }, "Q."), el("span", {}, question)]);
    const sources = el("div", { class: "ask-sources" });
    const answer = el("div", { class: "ask-answer" }, [el("span", { class: "ask-a-label" }, "A."), el("div", { class: "ask-answer-body markdown", html: '<span class="ask-pending">retrieving…</span>' })]);
    card.append(q, sources, answer);
    convo.prepend(card);
    return { card, sources, answer };
  }

  function renderSources(parentEl, sources) {
    if (!sources || !sources.length) return;
    parentEl.innerHTML = "";
    const header = el("h4", {}, `sources · ${sources.length}`);
    parentEl.appendChild(header);
    const list = el("ol", { class: "ask-source-list" });
    for (const s of sources) {
      const li = el("li", { class: "ask-source-card" });
      li.appendChild(el("a", { class: "cite", href: `/turns/${s.id}` }, `#${s.id}`));
      li.appendChild(el("span", { class: "ask-source-meta" }, ` ${(s.started_at || '').slice(0,19)} · ${s.model || '—'}`));
      li.appendChild(el("div", { class: "ask-source-snippet" }, s.prompt_snippet || ""));
      list.appendChild(li);
    }
    parentEl.appendChild(list);
  }

  async function ask(question) {
    const { sources: srcEl, answer: ansEl } = appendExchange(question);
    const body = ansEl.querySelector(".ask-answer-body");
    body.innerHTML = '<span class="ask-pending">planning…</span>';
    status.textContent = "planning…";

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
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split("\n");
        buf = lines.pop() || "";
        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          let parsed;
          try { parsed = JSON.parse(line.slice(6)); } catch { continue; }
          const { event, data } = parsed;
          if (event === "plan")        status.textContent = `retrieving · k=${data.k}`;
          else if (event === "sources") {
            status.textContent = `synthesising · ${data.length} sources`;
            renderSources(srcEl, data);
            body.innerHTML = '<span class="ask-pending">synthesising…</span>';
          }
          else if (event === "delta") {
            answerMd += data;
            body.innerHTML = linkifyCitations(answerMd).replace(/\n\n/g, "<br><br>");
          }
          else if (event === "done") {
            const c = data.cost_usd_today;
            status.textContent = `done · today $${typeof c === "number" ? c.toFixed(4) : "—"}`;
          }
          else if (event === "error") {
            body.innerHTML = `<span class="ask-error">${data}</span>`;
            status.textContent = "error";
          }
        }
      }
    } catch (e) {
      body.innerHTML = `<span class="ask-error">${e.message}</span>`;
      status.textContent = "error";
    }
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = input.value.trim();
    if (!q) return;
    input.value = "";
    ask(q);
  });
})();
