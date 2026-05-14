// Smart rerun queue — Vercel-mode state + budget preview.
(() => {
  const form = document.getElementById("queue-form");
  if (!form) return;
  const boxes = form.querySelectorAll(".queue-checkbox");
  const count = document.getElementById("queue-count");
  const cost  = document.getElementById("queue-cost");
  const fill  = document.getElementById("queue-budget-fill");
  const submit = document.getElementById("queue-submit");
  const selectAll = document.getElementById("queue-select-all");
  const clearBtn = document.getElementById("queue-clear");

  const CAP = 10.00;

  function recompute() {
    let n = 0, total = 0;
    for (const b of boxes) {
      if (b.checked) {
        n += 1;
        const c = parseFloat(b.closest(".queue-card").dataset.cost || "0");
        total += c;
        b.closest(".queue-card").classList.add("queue-card-checked");
      } else {
        b.closest(".queue-card").classList.remove("queue-card-checked");
      }
    }
    count.textContent = String(n);
    cost.textContent = "$" + total.toFixed(2);
    const pct = Math.min(100, (total / CAP) * 100);
    fill.style.width = pct + "%";
    fill.classList.toggle("over", total > CAP);
    submit.disabled = n === 0 || total > CAP;
    submit.querySelector(".queue-submit-text").textContent =
      n === 0 ? "rerun selected" :
      total > CAP ? "over budget" :
      `rerun selected (${n})`;
  }
  for (const b of boxes) b.addEventListener("change", recompute);
  selectAll.addEventListener("click", () => { for (const b of boxes) b.checked = true; recompute(); });
  clearBtn .addEventListener("click", () => { for (const b of boxes) b.checked = false; recompute(); });
  recompute();
})();
