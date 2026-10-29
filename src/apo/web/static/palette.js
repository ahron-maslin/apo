(function (root) {
  "use strict";

  // ⌘K: jump anywhere, or act. The parsed intent is always shown before Enter, so a
  // terse grammar ("a 12 2") stays safe.
  //
  //   accept 12            accept #12's recommendation       (alias: y 12)
  //   answer 12 2          pick option 2 of #12               (alias: a 12 2)
  //   answer 12 use v2     answer #12 in your own words
  //   done 12              confirm an Action done              (alias: e 12)
  //   decide 12            let the agent decide
  //   decline 12 [note]    decline
  //   snooze 12 tomorrow   2h | tonight | tomorrow | week | blocks
  //   seen                 mark "while you were away" seen
  //   go <page>            now | sessions | decisions | activity | settings
  //   anything else        fuzzy search: pages, projects, #requests, sessions

  var SNOOZES = { "2h": "2h", tonight: "tonight", tomorrow: "tomorrow", week: "week", blocks: "until_blocks" };
  var SNOOZE_LABELS = { "2h": "for 2 hours", tonight: "until tonight", tomorrow: "until tomorrow 9:00",
                        week: "until next week", until_blocks: "until it blocks" };
  var VERBS = { accept: "accept", y: "accept", answer: "answer", a: "answer", done: "done", e: "done",
                decide: "decide", decline: "decline", snooze: "snooze" };

  function request(data, id) {
    return (data.requests || []).filter(function (r) { return r.id === id; })[0] || null;
  }

  function quote(r) { return "#" + r.id + " “" + r.title + "”"; }

  function answerPost(id, fields) {
    return { method: "post", action: "/decisions/" + id + "/answer", fields: fields };
  }

  // Returns {label, run} for a command, {label, error} for a malformed one, or null
  // when the text isn't a command at all (then it's a search).
  function parseCommand(text, data) {
    var words = text.trim().split(/\s+/);
    var first = (words[0] || "").toLowerCase();
    if (first === "seen" && words.length === 1) {
      return { label: "Mark “while you were away” seen", run: { method: "post", action: "/seen", fields: {} } };
    }
    if (first === "go" && words.length === 2) {
      var page = (data.pages || []).filter(function (p) { return p.label.toLowerCase().indexOf(words[1].toLowerCase()) === 0; })[0];
      return page ? { label: "Go to " + page.label, run: { href: page.href } } : { label: "No page called " + words[1], error: true };
    }
    if (first === "search" && words.length > 1) {
      var q = words.slice(1).join(" ");
      return { label: "Search everything for “" + q + "”", run: { href: "/search?q=" + encodeURIComponent(q) } };
    }
    if (first === "bug" && words.length > 2) {
      var name = words[1].toLowerCase();
      var project = (data.projects || []).filter(function (p) { return p.name.toLowerCase() === name; })[0]
        || (data.projects || []).filter(function (p) { return p.name.toLowerCase().indexOf(name) === 0; })[0];
      if (!project || !project.id) return { label: "No project called " + words[1], error: true };
      var bugTitle = words.slice(2).join(" ");
      return { label: "Report a bug to " + project.name + ": " + bugTitle,
               run: { method: "post", action: "/projects/" + project.id + "/items", fields: { kind: "bug", title: bugTitle } } };
    }
    if (first === "accept" && (words[1] || "").toLowerCase() === "all") {
      return { label: "Review sure, reversible recommendations to accept in bulk", run: { href: "/accept" } };
    }
    if ((first === "plan" || first === "round") && (words.length === 1 || /^\d+$/.test(words[1]))) {
      return { label: words[1] ? "Lightning round: " + words[1] + " minutes" : "Lightning round: everything",
               run: { href: words[1] ? "/round?minutes=" + words[1] : "/round" } };
    }
    if (first === "apply" && words.length === 2 && /^#?\d+$/.test(words[1])) {
      var n = words[1].replace("#", "");
      return { label: "Apply the answer to #" + n + " in a background session",
               run: { method: "post", action: "/decisions/" + n + "/apply", fields: {} } };
    }
    var verb = VERBS[first];
    if (!verb || words.length < 2 || !/^#?\d+$/.test(words[1])) return null;
    var id = parseInt(words[1].replace("#", ""), 10);
    var r = request(data, id);
    if (!r) return { label: "#" + id + " isn't an open request", error: true };
    var rest = words.slice(2).join(" ");

    if (verb === "accept") {
      if (!r.recommendation) return { label: quote(r) + " has no recommendation to accept", error: true };
      return { label: "Accept the recommendation on " + quote(r) + ": " + r.recommendation,
               run: answerPost(id, { choice: "recommendation", answer_text: rest }) };
    }
    if (verb === "answer") {
      if (!rest) return { label: "Answer " + quote(r) + " with an option number or your own words", error: true };
      if (/^\d+$/.test(rest) && r.options && r.options.length) {
        var n = parseInt(rest, 10);
        if (n < 1 || n > r.options.length) return { label: quote(r) + " has options 1–" + r.options.length, error: true };
        return { label: "Answer " + quote(r) + " → option " + n + ": " + r.options[n - 1],
                 run: answerPost(id, { option: String(n - 1) }) };
      }
      return { label: "Answer " + quote(r) + ": " + rest, run: answerPost(id, { answer_text: rest }) };
    }
    if (verb === "done") {
      return { label: "Mark " + quote(r) + " done" + (rest ? ": " + rest : ""),
               run: answerPost(id, { choice: "done", answer_text: rest }) };
    }
    if (verb === "decide") {
      return { label: "Let the agent decide " + quote(r), run: answerPost(id, { choice: "delegate", answer_text: rest }) };
    }
    if (verb === "decline") {
      return { label: "Decline " + quote(r) + (rest ? ": " + rest : ""), run: answerPost(id, { choice: "decline", answer_text: rest }) };
    }
    if (verb === "snooze") {
      var preset = SNOOZES[rest.toLowerCase()];
      if (!preset) return { label: "Snooze " + quote(r) + " for: 2h, tonight, tomorrow, week, or blocks", error: true };
      return { label: "Snooze " + quote(r) + " " + SNOOZE_LABELS[preset],
               run: { method: "post", action: "/decisions/" + id + "/snooze", fields: { preset: preset } } };
    }
    return null;
  }

  // Subsequence match, rewarding prefixes and word starts. -1 when it doesn't match.
  function fuzzyScore(query, text) {
    query = query.toLowerCase();
    text = (text || "").toLowerCase();
    if (!query) return 0;
    if (text.indexOf(query) === 0) return 1000 - text.length;
    var at = text.indexOf(query);
    if (at > 0) return 500 - at;
    var score = 0, ti = 0;
    for (var qi = 0; qi < query.length; qi++) {
      var found = text.indexOf(query[qi], ti);
      if (found < 0) return -1;
      score += found === ti ? 3 : (found === 0 || text[found - 1] === " " ? 2 : 1);
      ti = found + 1;
    }
    return score;
  }

  function search(query, data, limit) {
    var entries = [];
    (data.pages || []).forEach(function (p) { entries.push({ label: p.label, sub: "Page", href: p.href }); });
    (data.views || []).forEach(function (v) { entries.push({ label: v.name, sub: "Saved view", href: v.href }); });
    (data.requests || []).forEach(function (r) {
      entries.push({ label: "#" + r.id + " " + r.title, sub: (r.project || "") + " · open " + r.kind, href: "/decisions/" + r.id });
    });
    (data.projects || []).forEach(function (p) {
      entries.push({ label: p.name, sub: "Project", href: p.id ? "/projects/" + p.id : "/decisions?project=" + encodeURIComponent(p.name) });
      entries.push({ label: p.name + " now", sub: "Project · what needs you", href: "/?project=" + encodeURIComponent(p.name) });
      entries.push({ label: p.name + " activity", sub: "Project · activity",
                     href: p.id ? "/projects/" + p.id + "/activity" : "/projects/activity" });
    });
    (data.sessions || []).forEach(function (s) {
      entries.push({ label: s.title, sub: "Session · " + (s.project || "no project") + " · " + s.status, href: s.href });
    });
    var q = query.trim().replace(/^#(?=\d)/, "#");
    return entries
      .map(function (e) { return { e: e, s: fuzzyScore(q, e.label) }; })
      .filter(function (x) { return x.s >= 0; })
      .sort(function (a, b) { return b.s - a.s; })
      .slice(0, limit || 8)
      .map(function (x) { return x.e; });
  }

  var api = { parseCommand: parseCommand, fuzzyScore: fuzzyScore, search: search };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
    return;
  }
  root.Palette = api;

  // --- UI ---
  var dialog = document.querySelector("[data-palette]");
  if (!dialog) return;
  var input = dialog.querySelector("[data-palette-input]");
  var list = dialog.querySelector("[data-palette-results]");
  var data = null, results = [], selected = 0;

  function load() {
    return fetch("/api/palette").then(function (r) { return r.json(); }).then(function (d) { data = d; render(); });
  }

  function execute(run) {
    if (run.href) {
      window.location.href = run.href;
      return;
    }
    var form = document.createElement("form");
    form.method = "post";
    form.action = run.action;
    var fields = Object.assign({ next: window.location.pathname + window.location.search }, run.fields);
    Object.keys(fields).forEach(function (name) {
      var field = document.createElement("input");
      field.type = "hidden";
      field.name = name;
      field.value = fields[name];
      form.appendChild(field);
    });
    document.body.appendChild(form);
    form.submit();
  }

  function render() {
    if (!data) return;
    var text = input.value;
    var command = parseCommand(text, data);
    results = [];
    if (command) results.push({ label: command.label, sub: command.error ? "Not a complete command" : "↵ to do this",
                                run: command.error ? null : command.run, command: true, error: command.error });
    if (!command || command.error) {
      search(text, data).forEach(function (e) { results.push({ label: e.label, sub: e.sub, run: { href: e.href } }); });
      if (text.trim()) {
        results.push({ label: "Search everything for \u201c" + text.trim() + "\u201d", sub: "Items, recaps, outcomes",
                       run: { href: "/search?q=" + encodeURIComponent(text.trim()) } });
      }
    }
    selected = Math.min(selected, Math.max(0, results.length - 1));
    list.innerHTML = "";
    results.forEach(function (r, i) {
      var item = document.createElement("li");
      item.className = "palette-item" + (i === selected ? " is-selected" : "") + (r.command ? " is-command" : "") + (r.error ? " is-error" : "");
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", i === selected ? "true" : "false");
      var label = document.createElement("span");
      label.className = "palette-label";
      label.textContent = r.label;
      var sub = document.createElement("span");
      sub.className = "palette-sub";
      sub.textContent = r.sub;
      item.appendChild(label);
      item.appendChild(sub);
      item.addEventListener("mousedown", function (event) {
        event.preventDefault();
        if (r.run) execute(r.run);
      });
      list.appendChild(item);
    });
  }

  function open() {
    if (dialog.open) return;
    dialog.showModal();
    input.value = "";
    selected = 0;
    load();
    input.focus();
  }

  input.addEventListener("input", function () { selected = 0; render(); });
  input.addEventListener("keydown", function (event) {
    if (event.key === "ArrowDown") { selected = Math.min(selected + 1, results.length - 1); render(); event.preventDefault(); }
    else if (event.key === "ArrowUp") { selected = Math.max(selected - 1, 0); render(); event.preventDefault(); }
    else if (event.key === "Enter") {
      event.preventDefault();
      var r = results[selected];
      if (r && r.run) execute(r.run);
    }
  });
  dialog.addEventListener("click", function (event) { if (event.target === dialog) dialog.close(); });

  document.addEventListener("keydown", function (event) {
    var typing = document.activeElement && (document.activeElement.isContentEditable ||
      /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName));
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); open(); }
    else if (event.key === ":" && !typing && !dialog.open) { event.preventDefault(); open(); }
  });
  document.querySelectorAll("[data-palette-open]").forEach(function (b) { b.addEventListener("click", open); });
})(this);
