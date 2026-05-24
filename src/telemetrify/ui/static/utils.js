/* Tiny shared utils used by pulse.js and dashboard.js.
   Attached to window.TM to avoid module bundling. */
(function () {
  function _normIso(iso) {
    if (!iso) return null;
    return iso.includes("T") ? iso : iso.replace(" ", "T") + "Z";
  }

  // "just now" / "3 min ago" / "2 hr ago" / "yesterday" / "{N} days ago"
  // `short` returns a tight glanceable form for the topbar chip.
  function formatTimeAgo(iso, short) {
    const norm = _normIso(iso);
    if (!norm) return null;
    const then = new Date(norm);
    if (Number.isNaN(then.getTime())) return null;
    const diffMs = Date.now() - then.getTime();
    const minutes = Math.max(0, Math.round(diffMs / 60000));
    if (short) {
      if (minutes < 1)   return "just now";
      if (minutes < 60)  return `${minutes}m ago`;
      const hours = Math.round(minutes / 60);
      if (hours < 24)    return `${hours}h ago`;
      const days = Math.round(hours / 24);
      return `${days}d ago`;
    }
    if (minutes < 1)   return "just now";
    if (minutes < 60)  return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24)    return `${hours} hour${hours === 1 ? "" : "s"} ago`;
    const days = Math.round(hours / 24);
    if (days === 1)    return "yesterday";
    return `${days} days ago`;
  }

  function formatCompact(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    const abs = Math.abs(n);
    if (abs >= 1_000_000) {
      const v = n / 1_000_000;
      return `${v >= 10 ? v.toFixed(1).replace(/\.0$/, "") : v.toFixed(2).replace(/\.?0+$/, "")}M`;
    }
    if (abs >= 10_000) return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}K`;
    return n.toLocaleString("en-US");
  }

  function formatWithCommas(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "—";
    return Number(n).toLocaleString("en-US");
  }

  window.TM = Object.assign(window.TM || {}, {
    formatTimeAgo,
    formatCompact,
    formatWithCommas,
  });
})();
