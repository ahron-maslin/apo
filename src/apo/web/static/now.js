(function () {
  "use strict";

  // The queue re-sorts only at natural pauses — page load, a returned-to tab, a long
  // idle, or when asked — never under the pointer. (Paperclip's
  // useInboxSortAttention does the same.) Everything else is a pill saying there's
  // something new.
  var POLL_MS = 10000;
  var IDLE_APPLY_MS = 150000;
  var HIDDEN_APPLY_MS = 30000;
  var LANES = ["problems", "your_move", "asks", "in_transit", "review"];

  var root = document.querySelector("[data-now]");
  if (!root) return;
  var pill = document.querySelector("[data-updates]");
  var lastInteraction = Date.now();
  var hiddenSince = null;
  var pending = false;

  function renderedKeys() {
    return Array.from(document.querySelectorAll("[data-now-row]")).map(function (r) { return r.dataset.key; }).join(",");
  }

  function freshKeys(data) {
    var keys = [];
    LANES.forEach(function (lane) { (data[lane] || []).forEach(function (i) { keys.push(i.key); }); });
    return keys.join(",");
  }

  function composing() {
    return Array.from(document.querySelectorAll("textarea")).some(function (t) { return t.value.trim(); });
  }

  function apply(force) {
    if (!force && composing()) return;
    window.location.reload();
  }

  function setCount(count) {
    document.title = (count ? "(" + count + ") " : "") + "Now — apo";
    var badge = document.querySelector('[data-nav-count="now"]');
    if (badge) {
      badge.textContent = count;
      badge.hidden = !count;
    }
  }

  async function poll() {
    if (document.hidden) return;
    var data;
    try {
      var res = await fetch("/api/now" + location.search);
      if (!res.ok) return;
      data = await res.json();
    } catch (e) {
      return;
    }
    setCount(data.count);
    if (freshKeys(data) === renderedKeys()) {
      pending = false;
      if (pill) pill.hidden = true;
      return;
    }
    pending = true;
    if (pill) pill.hidden = false;
    if (Date.now() - lastInteraction > IDLE_APPLY_MS) apply(false);
  }

  ["pointerdown", "keydown", "scroll", "focusin"].forEach(function (name) {
    document.addEventListener(name, function () { lastInteraction = Date.now(); }, { passive: true });
  });

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      hiddenSince = Date.now();
      return;
    }
    var wasAway = hiddenSince !== null && Date.now() - hiddenSince >= HIDDEN_APPLY_MS;
    hiddenSince = null;
    poll().then(function () { if (pending && wasAway) apply(false); });
  });

  if (pill) pill.addEventListener("click", function () { apply(true); });

  // Keyboard triage. Every key clicks the selected row's own control, so a key does
  // exactly what its button does.
  var rows = function () { return Array.from(document.querySelectorAll("[data-now-row]")); };
  var current = -1;

  function select(index) {
    var all = rows();
    if (!all.length) return;
    current = Math.max(0, Math.min(index, all.length - 1));
    all.forEach(function (row, i) { row.classList.toggle("is-selected", i === current); });
    all[current].scrollIntoView({ block: "nearest" });
  }

  function selectedRow() {
    var all = rows();
    return current >= 0 && current < all.length ? all[current] : null;
  }

  function click(row, selector) {
    var target = row && row.querySelector(selector);
    if (target) target.click();
    return !!target;
  }

  function openDetails(row, selector) {
    var details = row && row.querySelector(selector);
    if (!details) return null;
    details.open = true;
    return details;
  }

  document.addEventListener("keydown", function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    var active = document.activeElement;
    if (active && (active.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName))) return;
    if (document.querySelector("dialog[open]")) return;
    // Enter on a focused button or link already does that control's own thing.
    if (event.key === "Enter" && active && /^(BUTTON|A|SUMMARY)$/.test(active.tagName)) return;
    var row = selectedRow();
    var handled = true;
    if (event.key === "r") {
      if (pending) apply(true);
    } else if (event.key === "j") {
      select(current + 1);
    } else if (event.key === "k") {
      select(current - 1);
    } else if (event.key === "Enter" && row) {
      click(row, ".now-title[href]");
    } else if (/^[1-9]$/.test(event.key) && row) {
      handled = click(row, 'button[name="option"][value="' + (parseInt(event.key, 10) - 1) + '"]');
    } else if (event.key === "y" && row) {
      handled = click(row, 'button[value="recommendation"]');
    } else if (event.key === "e" && row) {
      handled = row.classList.contains("kind-action") || row.classList.contains("kind-review")
        ? click(row, "[data-now-primary]") : false;
    } else if (event.key === "a" && row) {
      var details = openDetails(row, "details.now-answer");
      var box = details && details.querySelector("textarea");
      if (box) window.focusWhenReady(box);
      handled = !!box;
    } else if (event.key === "s" && row) {
      var snooze = openDetails(row, "details.now-snooze");
      var firstOption = snooze && snooze.querySelector("button");
      if (firstOption) window.focusWhenReady(firstOption);
      handled = !!snooze;
    } else if (event.key === "c" && row) {
      handled = click(row, "[data-copy]");
    } else {
      handled = false;
    }
    if (handled) event.preventDefault();
  });

  App.onChange(poll);
  setInterval(poll, POLL_MS);
})();
