// Keeps a page's [data-live-region] parts current: when the server says something
// changed (and every 15 seconds anyway), while the tab is visible, it refetches the
// page and swaps any region that changed — never one you're typing in.
(function () {
  if (!document.querySelector("[data-live-region]")) return;
  var POLL_MS = 15000;

  function busy(region) {
    var active = document.activeElement;
    if (active && region.contains(active) && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return true;
    if (region.querySelector('input[name="ids"]:checked')) return true;
    return Array.from(region.querySelectorAll("textarea, input[type=text]")).some(function (f) { return f.value; });
  }

  async function refresh() {
    if (document.visibilityState !== "visible") return;
    try {
      var res = await fetch(location.href, { headers: { Accept: "text/html" } });
      if (!res.ok) return;
      var doc = new DOMParser().parseFromString(await res.text(), "text/html");
      document.querySelectorAll("[data-live-region]").forEach(function (region) {
        var next = doc.querySelector('[data-live-region="' + region.dataset.liveRegion + '"]');
        if (!next || next.innerHTML === region.innerHTML || busy(region)) return;
        // Reopen what was open: a disclosure by its own id, else the one at the same place in its nearest owner with an id.
        var open = Array.from(region.querySelectorAll("details[open]")).map(function (d) {
          if (d.id) return { id: d.id };
          var owner = d.closest("[id]");
          return owner ? { owner: owner.id, at: Array.from(owner.querySelectorAll("details")).indexOf(d) } : null;
        });
        region.innerHTML = next.innerHTML;
        open.forEach(function (spot) {
          var details = null;
          if (spot && spot.id) details = document.getElementById(spot.id);
          else if (spot) {
            var owner = document.getElementById(spot.owner);
            details = owner && owner.querySelectorAll("details")[spot.at];
          }
          if (details) details.open = true;
        });
        if (window.App && App.autoEnhance) App.autoEnhance(region);
      });
    } catch (e) {}
  }

  App.onChange(refresh);
  setInterval(refresh, POLL_MS);
})();
