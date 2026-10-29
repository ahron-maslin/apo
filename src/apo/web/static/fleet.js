(function () {
  "use strict";

  var POLL_MS = 5000;
  var lastIds = null;
  var refreshInFlight = false;

  function container() {
    return document.getElementById("fleet-sections");
  }

  function cardSignature(section) {
    return Array.from(section.querySelectorAll("[data-session-id]"))
      .map(function (el) { return el.dataset.sessionId + ":" + el.dataset.status; })
      .sort()
      .join(",");
  }

  // Replacing a section would wipe whatever the person is typing into its
  // add-step field, so a section with a focused, non-empty input waits for a
  // later tick instead.
  function isBusy(section) {
    var active = document.activeElement;
    return !!(active && section.contains(active) && active.value);
  }

  function flash(el) {
    el.classList.remove("is-updated");
    void el.offsetWidth;
    el.classList.add("is-updated");
  }

  async function reconcileRoster() {
    var res = await fetch(location.pathname);
    if (!res.ok) return;
    var parsed = new DOMParser().parseFromString(await res.text(), "text/html");
    var root = container();
    var parsedRoot = parsed.getElementById("fleet-sections");
    if (!root || !parsedRoot) return;

    var parsedByProject = new Map(
      Array.from(parsedRoot.querySelectorAll(".fleet-project")).map(function (s) { return [s.dataset.project, s]; })
    );
    var changes = [];

    Array.from(root.querySelectorAll(".fleet-project")).forEach(function (section) {
      var project = section.dataset.project;
      var next = parsedByProject.get(project);
      parsedByProject.delete(project);
      if (!next) {
        section.remove();
        changes.push(project + " has no sessions left");
        return;
      }
      if (cardSignature(section) === cardSignature(next) || isBusy(section)) return;
      var before = new Map(
        Array.from(section.querySelectorAll("[data-session-id]")).map(function (c) { return [c.dataset.sessionId, c]; })
      );
      var clone = document.importNode(next, true);
      clone.querySelectorAll("[data-session-id]").forEach(function (card) {
        var old = before.get(card.dataset.sessionId);
        if (old) {
          var oldDetails = old.querySelector("details.checklist");
          var newDetails = card.querySelector("details.checklist");
          if (oldDetails && newDetails) newDetails.open = oldDetails.open;
        }
        if (!old || old.dataset.status !== card.dataset.status) flash(card);
      });
      section.replaceWith(clone);
      App.autoEnhance(clone);
      changes.push(project + "'s sessions changed");
    });

    // Anything left is a section that didn't exist before — including the
    // Unregistered / Recently ended buckets, matched the same way by data-project.
    parsedByProject.forEach(function (section, project) {
      var clone = document.importNode(section, true);
      root.appendChild(clone);
      App.autoEnhance(clone);
      changes.push(project + " has a new session");
    });

    if (!root.querySelector(".fleet-project") && parsedRoot.querySelector(".empty")) {
      root.innerHTML = parsedRoot.innerHTML;
    }
    if (changes.length) App.announce(changes.join("; "));
  }

  function cardFor(s) {
    return document.querySelector('[data-session-id="' + CSS.escape(s.claude_session_id) + '"]');
  }

  function setText(card, selector, text, hideSelector) {
    var el = card.querySelector(selector);
    if (el) el.textContent = text || "";
    var box = card.querySelector(hideSelector || selector);
    if (box) box.hidden = !text;
  }

  function setRecap(card, s) {
    setText(card, ".session-title", s.title);
    setText(card, ".session-prompt-text", s.last_prompt, ".session-prompt");
    setText(card, ".session-summary-text", s.last_summary, ".session-summary");
    setText(card, ".session-summary-when", s.last_summary_human);
    setText(card, ".context-size", s.context_label);
    var hasRecap = !!(s.last_summary || s.last_prompt);
    var recap = card.querySelector(".session-recap");
    if (recap) recap.hidden = !hasRecap;
    // The fallback path line only exists for a card with no recap yet — once
    // one arrives (typically the session's first Stop), it's redundant.
    var path = card.querySelector(".session-path");
    if (path && hasRecap) path.hidden = true;
    var prompt = card.querySelector(".session-prompt-text");
    if (prompt) prompt.title = s.last_prompt || "";
    App.initRecapToggles(card);
  }

  function updateStats(sessions) {
    var counts = { needs_you: 0, running: 0, idle: 0, stale: 0 };
    sessions.forEach(function (s) {
      if (s.needs_you) counts.needs_you++;
      if (s.display_status === "running") counts.running++;
      if (s.display_status === "idle") counts.idle++;
      if (s.stale) counts.stale++;
    });
    Object.keys(counts).forEach(function (key) {
      var el = document.querySelector('[data-stat="' + key + '"]');
      if (el && el.textContent !== String(counts[key])) el.textContent = counts[key];
    });
    var box = document.querySelector('[data-stat-box="needs_you"]');
    if (box) box.dataset.active = counts.needs_you ? "true" : "false";
    var navBadge = document.querySelector('[data-nav-count="fleet"]');
    if (navBadge) {
      navBadge.textContent = counts.needs_you;
      navBadge.hidden = !counts.needs_you;
    }
  }

  // "Live" must not stay green over data that stopped refreshing.
  function setLive(ok) {
    var indicator = document.querySelector(".live-indicator");
    if (!indicator) return;
    indicator.dataset.state = ok ? "live" : "stale";
    var dot = indicator.querySelector(".dot");
    if (dot) {
      dot.classList.toggle("tone-success", ok);
      dot.classList.toggle("dot--live", ok);
      dot.classList.toggle("tone-neutral", !ok);
    }
    var label = indicator.querySelector("[data-live-label]");
    if (label) label.textContent = ok ? "Live" : "Reconnecting…";
    indicator.title = ok ? "Refreshes every 5 seconds" : "Couldn't reach the dashboard server — showing the last data it sent";
  }

  async function refreshFleet() {
    if (refreshInFlight || document.hidden) return;
    refreshInFlight = true;
    try {
      var res = await fetch("/api/sessions");
      if (!res.ok) { setLive(false); return; }
      var sessions = await res.json();
      setLive(true);
      updateStats(sessions);

      var ids = sessions.map(function (s) { return s.claude_session_id; }).sort().join(",");
      var changed = sessions.filter(function (s) {
        var card = cardFor(s);
        return card && card.dataset.status !== s.display_status;
      });
      if ((lastIds !== null && lastIds !== ids) || changed.length) {
        await reconcileRoster();
        changed.forEach(function (s) {
          App.announce((s.repository_name || s.cwd || "A session") + " is now " + s.status_label);
        });
      }
      lastIds = ids;

      sessions.forEach(function (s) {
        var card = cardFor(s);
        if (!card) return;
        card.title = (s.repository_name || s.cwd || "") + " — last seen " + s.last_seen_human;
        setRecap(card, s);
      });
    } catch (e) {
      setLive(false);
    } finally {
      refreshInFlight = false;
    }
  }

  document.addEventListener("visibilitychange", function () { if (!document.hidden) refreshFleet(); });
  App.onChange(refreshFleet, 1000);
  setInterval(refreshFleet, POLL_MS);
})();
