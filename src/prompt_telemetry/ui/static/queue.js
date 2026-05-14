// Smart rerun queue — checkbox state + budget preview.
(() => {
  const form = document.getElementById("queue-form");
  if (!form) return;
  const boxes = form.querySelectorAll(".queue-checkbox");
  const count = document.getElementById("queue-count");
  const cost  = document.getElementById("queue-cost");
  const submit = document.getElementById("queue-submit");
  const selectAll = document.getElementById("queue-select-all");
  const clearBtn = document.getElementById("queue-clear");

  function recompute() {
    let n = 0; let total = 0;
    for (const b of boxes) {
      if (b.checked) {
        n += 1;
        const c = parseFloat(b.closest(".queue-card").dataset.cost || "0");
        total += c;
      }
    }
    count.textContent = String(n);
    cost.textContent = "$" + total.toFixed(2);
    submit.disabled = n === 0;
    submit.textContent = n ? `rerun selected (${n})` : "rerun selected";
  }
  for (const b of boxes) b.addEventListener("change", recompute);
  selectAll.addEventListener("click", () => { for (const b of boxes) b.checked = true; recompute(); });
  clearBtn .addEventListener("click", () => { for (const b of boxes) b.checked = false; recompute(); });
  recompute();
})();
