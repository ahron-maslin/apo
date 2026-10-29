// Keeps a page's [data-live-region] parts current: when the server says something
// changed (and every 15 seconds anyway), while the tab is visible, it refetches the
// page and swaps any region that changed — never one you're typing in.
(function () {
  if (!document.querySelector("[data-live-region]")) return;
  var POLL_MS = 15000;

  function busy(region) {
    var active = document.activeElement;
    if (active && region.contains(active) && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return true;
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
        var open = Array.from(region.querySelectorAll("details[open]")).map(function (d) {
          var owner = d.closest("[id]");
          return owner ? owner.id : null;
        });
        region.innerHTML = next.innerHTML;
        open.forEach(function (id) {
          var owner = id && document.getElementById(id);
          var details = owner && owner.querySelector("details");
          if (details) details.open = true;
        });
        if (window.App && App.autoEnhance) App.autoEnhance(region);
      });
    } catch (e) {}
  }

  App.onChange(refresh);
  setInterval(refresh, POLL_MS);
})();
