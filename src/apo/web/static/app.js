(function () {
  "use strict";

  var IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

  function iconSvg(name) {
    return '<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-' + name + '"/></svg>';
  }

  function ensureLiveRegions() {
    if (!document.getElementById("app-toast-stack")) {
      var stack = document.createElement("div");
      stack.id = "app-toast-stack";
      stack.className = "toast-stack";
      stack.setAttribute("aria-live", "polite");
      document.body.appendChild(stack);
    }
    if (!document.getElementById("app-live-region")) {
      var region = document.createElement("div");
      region.id = "app-live-region";
      region.className = "sr-only";
      region.setAttribute("role", "status");
      region.setAttribute("aria-live", "polite");
      document.body.appendChild(region);
    }
  }

  // --- Toasts ---

  var TOAST_ICONS = { success: "check-circle", error: "alert", info: "info" };
  var TOAST_TONES = { success: "tone-success", error: "tone-danger", info: "tone-accent" };

  function dismissToast(el) {
    if (!el || el.classList.contains("is-leaving")) return;
    el.classList.add("is-leaving");
    var done = function () { el.remove(); };
    el.addEventListener("animationend", done, { once: true });
    setTimeout(done, 400);
  }

  function toast(message, type, duration) {
    ensureLiveRegions();
    type = type || "info";
    duration = duration || (type === "error" ? 6000 : 4000);
    var stack = document.getElementById("app-toast-stack");
    var el = document.createElement("div");
    el.className = "toast " + type + " " + (TOAST_TONES[type] || "");
    if (type === "error") el.setAttribute("role", "alert");
    el.innerHTML = iconSvg(TOAST_ICONS[type] || "info") + '<div class="toast-msg"></div>' +
      '<button type="button" class="btn btn--ghost btn--icon toast-close" aria-label="Dismiss">' + iconSvg("x") + "</button>";
    el.querySelector(".toast-msg").textContent = message;
    el.querySelector(".toast-close").addEventListener("click", function () { dismissToast(el); });
    stack.appendChild(el);
    var timer = setTimeout(function () { dismissToast(el); }, duration);
    el.addEventListener("mouseenter", function () { clearTimeout(timer); });
    el.addEventListener("mouseleave", function () { timer = setTimeout(function () { dismissToast(el); }, 2000); });
  }

  function announce(message) {
    ensureLiveRegions();
    var region = document.getElementById("app-live-region");
    // Clearing first forces assistive tech to re-announce when the text is
    // identical to what's already there (e.g. two sessions ending back to back).
    region.textContent = "";
    requestAnimationFrame(function () {
      region.textContent = message;
    });
  }

  // A message to show after a POST → redirect lands, keyed to the page it
  // should land on so an unrelated page (or an error response) never shows it.
  var FLASH_KEY = "apo-flash";

  function setFlash(message, path) {
    try { sessionStorage.setItem(FLASH_KEY, JSON.stringify({ message: message, path: path })); } catch (e) {}
  }

  function consumeFlash() {
    var raw;
    try {
      raw = sessionStorage.getItem(FLASH_KEY);
      sessionStorage.removeItem(FLASH_KEY);
    } catch (e) { return; }
    if (!raw) return;
    try {
      var flash = JSON.parse(raw);
      if (!flash.path || flash.path === location.pathname) toast(flash.message, "success");
    } catch (e) {}
  }

  // Done, Cancel, Snooze and answers leave a short-lived cookie naming the item; show an
  // Undo toast for it once, wherever the redirect landed.
  function consumeUndo() {
    var match = document.cookie.match(/(?:^|; )apo_undo=([^;]*)/);
    if (!match) return;
    document.cookie = "apo_undo=; Max-Age=0; path=/";
    var info;
    try { info = JSON.parse(decodeURIComponent(match[1])); } catch (e) { return; }
    toast(info.label + ".", "success", 8000);
    var stack = document.getElementById("app-toast-stack");
    var el = stack && stack.lastElementChild;
    if (!el) return;
    var undo = document.createElement("button");
    undo.type = "button";
    undo.className = "btn btn--secondary btn--sm toast-undo";
    undo.textContent = "Undo";
    undo.addEventListener("click", function () {
      undo.disabled = true;
      var body = new URLSearchParams({ next: location.pathname + location.search });
      fetch("/items/" + info.id + "/undo", { method: "POST", body: body })
        .then(function () { location.reload(); })
        .catch(function () { toast("Couldn't undo that.", "error"); });
    });
    el.insertBefore(undo, el.querySelector(".toast-close"));
  }

  // --- Loading state + form enhancement ---

  function setLoading(button, loadingText) {
    if (!button || button.dataset.loading === "1") return;
    button.dataset.loading = "1";
    button.dataset.originalHtml = button.innerHTML;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    var spinner = '<span class="spinner" aria-hidden="true"></span>';
    var label = loadingText === "" ? "" : "<span>" + (loadingText || "Working…") + "</span>";
    button.innerHTML = spinner + label;
  }

  function clearLoading(button) {
    if (!button || button.dataset.loading !== "1") return;
    button.innerHTML = button.dataset.originalHtml;
    button.disabled = false;
    button.removeAttribute("aria-busy");
    delete button.dataset.loading;
    delete button.dataset.originalHtml;
  }

  function submitButtonFor(form) {
    return form.querySelector('button[type="submit"], button:not([type])');
  }

  // Submit the way a click on `submitter` would — its name/value is often the
  // answer itself (an option, "done"), so it must travel with the form.
  function resubmit(form, submitter) {
    if (form.requestSubmit) {
      form.requestSubmit(submitter && submitter.form === form ? submitter : undefined);
      return;
    }
    if (submitter && submitter.name) {
      var carry = document.createElement("input");
      carry.type = "hidden";
      carry.name = submitter.name;
      carry.value = submitter.value;
      form.appendChild(carry);
    }
    form.submit();
  }

  function preserveDetailsState(oldEl, newEl) {
    var oldDetails = oldEl.querySelectorAll("details");
    var newDetails = newEl.querySelectorAll("details");
    for (var i = 0; i < oldDetails.length && i < newDetails.length; i++) {
      newDetails[i].open = oldDetails[i].open;
    }
  }

  function indicateSubmit(form, opts) {
    opts = opts || {};
    if (form.dataset.indicateBound === "1") return;
    form.dataset.indicateBound = "1";
    form.addEventListener("submit", function (event) {
      var button = event.submitter || submitButtonFor(form);
      // After the browser has built the form data: a button disabled during the
      // submit event is left out of it, and the clicked button IS the answer.
      setTimeout(function () { setLoading(button, opts.loadingText || form.dataset.loadingText); }, 0);
      if (form.dataset.flash) setFlash(form.dataset.flash, form.dataset.flashPath);
      // No preventDefault: the browser's normal POST+redirect navigation still
      // happens, this just gives the user feedback before the page unloads.
    });
  }

  function enhanceForm(form, opts) {
    opts = opts || {};
    if (form.dataset.enhanceBound === "1") return;
    form.dataset.enhanceBound = "1";
    var swapSelector = opts.swapTarget || form.dataset.swapTarget;

    form.addEventListener("submit", function (event) {
      if (form.dataset.bypassEnhance === "1") {
        delete form.dataset.bypassEnhance;
        return;
      }
      event.preventDefault();
      var submitter = event.submitter;
      var button = submitter || submitButtonFor(form);
      // Switches flip immediately; the swapped-in server markup is the truth.
      if (button && button.getAttribute("role") === "switch") {
        button.setAttribute("aria-checked", button.getAttribute("aria-checked") === "true" ? "false" : "true");
      }
      setLoading(button, opts.loadingText !== undefined ? opts.loadingText : form.dataset.loadingText);

      var body;
      try { body = new FormData(form, submitter || undefined); } catch (e) { body = new FormData(form); }
      fetch(form.action, { method: form.method || "POST", body: body })
        .then(function (response) {
          return response.text().then(function (html) {
            return { response: response, html: html };
          });
        })
        .then(function (result) {
          var parsed = new DOMParser().parseFromString(result.html, "text/html");
          var newEl = swapSelector ? parsed.querySelector(swapSelector) : null;
          var curEl = swapSelector ? document.querySelector(swapSelector) : null;

          if (newEl && curEl) {
            if (result.response.url && result.response.url !== location.href) {
              history.replaceState(null, "", result.response.url);
            }
            var inserted = document.importNode(newEl, true);
            preserveDetailsState(curEl, inserted);
            var hadFocus = curEl.contains(document.activeElement);
            curEl.replaceWith(inserted);
            autoEnhance(inserted);
            if (hadFocus) {
              var refocus = inserted.querySelector("form.add-item input, [role=switch]");
              if (refocus) refocus.focus();
            }
            if (opts.onSwap) opts.onSwap(inserted);
            consumeUndo();
            if (!result.response.ok) toast(opts.errorMessage || "That didn't save — check the form.", "error");
            else if (form.dataset.successToast) toast(form.dataset.successToast, "success");
          } else {
            location.href = result.response.url || form.action;
          }
        })
        .catch(function () {
          clearLoading(button);
          toast("Couldn't reach the server — retrying…", "error");
          form.dataset.bypassEnhance = "1";
          resubmit(form, submitter);
        });
    });
  }

  function autoEnhance(root) {
    root = root || document;
    var swapForms = root.querySelectorAll('form[data-enhance="swap"]');
    for (var i = 0; i < swapForms.length; i++) enhanceForm(swapForms[i]);
    var indicateForms = root.querySelectorAll('form[data-enhance="indicate"]');
    for (var j = 0; j < indicateForms.length; j++) indicateSubmit(indicateForms[j]);
    autoEnhanceAttachments(root);
    initSubmitShortcuts(root);
    initRecapToggles(root);
    document.dispatchEvent(new CustomEvent("app:enhance", { detail: root }));
  }

  // --- Live changes ---

  // The server says "change" the moment anything commits (a hook, another tab). Only a
  // visible tab holds the stream: a browser allows about six connections per host, and
  // a stream per background tab would starve the page you're looking at.
  var changeListeners = [];
  var changeStream = null;

  function openChangeStream() {
    if (changeStream || !changeListeners.length || document.hidden || !window.EventSource) return;
    changeStream = new EventSource("/api/stream");
    changeStream.addEventListener("change", notifyChange);
  }

  function notifyChange() {
    changeListeners.forEach(function (fn) { fn(); });
  }

  // fn runs at most every minGap ms, and once more after a burst so the last change shows.
  function onChange(fn, minGap) {
    var last = 0, timer = null;
    changeListeners.push(function () {
      var wait = last + (minGap || 1500) - Date.now();
      if (timer) return;
      if (wait <= 0) { last = Date.now(); fn(); return; }
      timer = setTimeout(function () { timer = null; last = Date.now(); fn(); }, wait);
    });
    openChangeStream();
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      if (changeStream) { changeStream.close(); changeStream = null; }
    } else if (changeListeners.length) {
      openChangeStream();
      notifyChange();
    }
  });

  // --- Session recaps ---

  var recapDialog = null;

  function ensureRecapDialog() {
    if (recapDialog) return recapDialog;
    recapDialog = document.createElement("dialog");
    recapDialog.className = "recap-modal";
    recapDialog.setAttribute("aria-labelledby", "recap-modal-title");
    recapDialog.innerHTML =
      '<form method="dialog">' +
      '<h2 id="recap-modal-title">Last done <span class="muted recap-modal-when"></span></h2>' +
      '<div class="recap-modal-prompt" hidden><div class="recap-label">You asked</div><p class="recap-modal-prompt-text"></p></div>' +
      '<div class="recap-modal-text"></div>' +
      '<div class="modal-actions"><button class="btn btn--secondary" value="close">Close</button></div>' +
      '</form>';
    recapDialog.addEventListener("click", function (event) {
      if (event.target === recapDialog) recapDialog.close();
    });
    document.body.appendChild(recapDialog);
    return recapDialog;
  }

  function openRecapDialog(card, text) {
    var dialog = ensureRecapDialog();
    var when = card.querySelector(".session-summary-when");
    var prompt = card.querySelector(".session-prompt-text");
    dialog.querySelector(".recap-modal-when").textContent = when ? when.textContent : "";
    dialog.querySelector(".recap-modal-text").textContent = text.textContent;
    dialog.querySelector(".recap-modal-prompt").hidden = !prompt || !prompt.textContent;
    dialog.querySelector(".recap-modal-prompt-text").textContent = prompt ? prompt.textContent : "";
    dialog.showModal();
    dialog.querySelector(".recap-modal-text").scrollTop = 0;
  }

  function initRecapToggles(root) {
    root.querySelectorAll("[data-recap-toggle]").forEach(function (btn) {
      var text = document.getElementById(btn.getAttribute("aria-controls"));
      if (!text) return;
      function measure() {
        btn.hidden = text.scrollHeight <= text.clientHeight + 1;
      }
      measure();
      if (btn.dataset.bound === "1") return;
      btn.dataset.bound = "1";
      // A card measured while hidden or before fonts load reads 0 and would never get its button.
      if (window.ResizeObserver) new ResizeObserver(measure).observe(text);
      btn.addEventListener("click", function () {
        openRecapDialog(btn.closest(".session-card, article, li") || btn.parentElement, text);
      });
    });
  }

  // --- Keyboard affordances ---

  function initSubmitShortcuts(root) {
    var fields = root.querySelectorAll("textarea[data-submit-shortcut]");
    for (var i = 0; i < fields.length; i++) {
      var field = fields[i];
      if (field.dataset.shortcutBound === "1") continue;
      field.dataset.shortcutBound = "1";
      field.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" || !(event.metaKey || event.ctrlKey)) return;
        var form = event.target.form;
        if (!form) return;
        event.preventDefault();
        if (form.reportValidity()) resubmit(form, form.querySelector("[data-default-submit]"));
      });
    }
    if (IS_MAC) {
      var keys = root.querySelectorAll("[data-mod-key]");
      for (var k = 0; k < keys.length; k++) keys[k].textContent = "⌘";
    }
  }

  document.addEventListener("click", function (event) {
    var source = event.target.closest && event.target.closest("[data-copy-url]");
    if (!source) return;
    fetch(source.getAttribute("data-copy-url")).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function (text) {
      var done = function () { toast("Status copied."); };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { window.prompt("Copy this:", text); });
      } else {
        window.prompt("Copy this:", text);
      }
    }).catch(function () { toast("Couldn't copy the status."); });
  });

  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy]");
    if (!button) return;
    var text = button.getAttribute("data-copy");
    var done = function () { toast("Copied — paste it in a terminal."); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { window.prompt("Copy this:", text); });
    } else {
      window.prompt("Copy this:", text);
    }
  });

  // Answers land verbatim in the repo's .claude/decisions/ — some repos commit them.
  // Mirrors db._SECRET_PATTERNS; the server refuses without this confirmation too.
  var SECRET_PATTERNS = [
    ["Stripe secret key", /\b[rs]k_(live|test)_[A-Za-z0-9]{10,}/],
    ["GitHub token", /\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})/],
    ["AWS access key", /\b(AKIA|ASIA)[0-9A-Z]{16}\b/],
    ["Slack token", /\bxox[abprs]-[A-Za-z0-9-]{10,}/],
    ["private key", /-----BEGIN [A-Z ]*PRIVATE KEY-----/],
    ["Google API key", /\bAIza[0-9A-Za-z_-]{35}\b/],
    ["Anthropic API key", /\bsk-ant-[A-Za-z0-9_-]{20,}/],
    ["OpenAI API key", /\bsk-(proj-)?[A-Za-z0-9]{32,}/]
  ];

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.hasAttribute || !form.hasAttribute("data-secret-check")) return;
    var box = form.querySelector('textarea[name="answer_text"]');
    var text = box ? box.value : "";
    var hit = SECRET_PATTERNS.filter(function (p) { return p[1].test(text); })[0];
    if (!hit) return;
    var ok = window.confirm(
      "This looks like it contains a " + hit[0] + ".\n\nAnswers are written into the repo's .claude/decisions/ " +
      "files, and some repos commit them. Better: put it in the project's .env or a secret manager and refer to it " +
      "by name.\n\nSend it anyway?"
    );
    if (!ok) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    var allow = document.createElement("input");
    allow.type = "hidden";
    allow.name = "allow_secret";
    allow.value = "1";
    form.appendChild(allow);
  }, true);

  // ? opens the shortcut list; g then a letter goes to a page.
  var pendingGo = 0;
  var GO = { n: "/", p: "/projects", o: "/projects#goals", w: "/projects/work", s: "/projects/agents",
             d: "/projects/decisions", a: "/projects/activity",
             f: "/search", h: "/guide", ",": "/settings" };
  document.addEventListener("keydown", function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey || isTypingTarget(document.activeElement)) return;
    if (document.querySelector("dialog[open]")) return;
    if (event.key === "?") {
      var help = document.getElementById("shortcuts");
      if (help && help.showModal) { event.preventDefault(); help.showModal(); }
      return;
    }
    if (event.key === "g") { pendingGo = Date.now(); return; }
    if (pendingGo && Date.now() - pendingGo < 1200 && GO[event.key]) {
      event.preventDefault();
      window.location.href = GO[event.key];
    }
    pendingGo = 0;
  });

  // "Need more info" / "I'm stuck" is only an answer once it says what's missing.
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-needs-note]");
    if (!button || !button.form) return;
    var note = button.form.querySelector('textarea[name="answer_text"]');
    if (note && !note.value.trim()) {
      event.preventDefault();
      note.focus();
      toast(button.dataset.needsNote || "Write what you need to know first — it goes back to the agent.", "error");
    }
  });

  // A <details> that was just opened animates its content in (content-visibility is
  // transitioned), and focus() on content that isn't rendered yet silently does nothing.
  window.focusWhenReady = function (el, left) {
    if (left === undefined) left = 40;
    el.focus();
    if (document.activeElement !== el && left > 0) {
      requestAnimationFrame(function () { window.focusWhenReady(el, left - 1); });
    }
  };

  function isTypingTarget(el) {
    return el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));
  }

  document.addEventListener("keydown", function (event) {
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    if (isTypingTarget(document.activeElement)) return;
    if (document.querySelector("dialog[open]")) return;
    event.preventDefault();
    var search = document.querySelector("[data-search-shortcut]");
    if (!search) { window.location.href = "/search"; return; }
    search.focus();
    search.select();
  });

  // <select data-navigate>: each option's value is a page to open.
  document.addEventListener("change", function (event) {
    var field = event.target;
    if (field.matches && field.matches("select[data-navigate]") && field.value) window.location.href = field.value;
  });

  // data-filter-list="#id" on an input: hides the list's [data-filter-text] items that don't match.
  document.addEventListener("input", function (event) {
    var field = event.target;
    if (!field.matches || !field.matches("[data-filter-list]")) return;
    var list = document.querySelector(field.dataset.filterList);
    if (!list) return;
    var query = field.value.trim().toLowerCase();
    list.querySelectorAll("[data-filter-text]").forEach(function (item) {
      item.hidden = !!query && item.dataset.filterText.indexOf(query) === -1;
    });
  });

  // Esc leaves a search box, so the list keys work on its results.
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && document.activeElement && document.activeElement.matches("[data-search-shortcut]")) {
      document.activeElement.blur();
    }
  });

  function plainKey(event) {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return false;
    if (isTypingTarget(document.activeElement)) return false;
    return !document.querySelector("dialog[open]");
  }

  // data-hotkey="x" on a button, link or field: pressing x clicks it (or focuses a field).
  document.addEventListener("keydown", function (event) {
    if (!plainKey(event) || event.key.length !== 1 || pendingGo) return;
    var target = document.querySelector('[data-hotkey="' + CSS.escape(event.key) + '"]');
    if (!target || target.closest("[hidden]")) return;
    event.preventDefault();
    var folded = target.closest("details");
    if (folded) folded.open = true;
    if (isTypingTarget(target)) window.focusWhenReady(target);
    else target.click();
  });

  // data-keylist: j/k move through its [data-keyitem]s, Enter or o opens one, p pins it, e marks it done.
  var listIndex = -1;
  function keyItems() {
    var list = document.querySelector("[data-keylist]");
    return list ? Array.from(list.querySelectorAll("[data-keyitem]")) : [];
  }
  function selectItem(index) {
    var items = keyItems();
    if (!items.length) return;
    listIndex = Math.max(0, Math.min(items.length - 1, index));
    items.forEach(function (el, i) { el.classList.toggle("is-key-selected", i === listIndex); });
    items[listIndex].scrollIntoView({ block: "nearest" });
  }
  document.addEventListener("keydown", function (event) {
    if (!plainKey(event) || !document.querySelector("[data-keylist]")) return;
    var items = keyItems();
    // A live refresh replaces the items and with them the highlight: act only on one still shown as selected.
    var current = items[listIndex] && items[listIndex].classList.contains("is-key-selected") ? items[listIndex] : null;
    if (event.key === "j" || event.key === "ArrowDown" && listIndex >= 0) selectItem(listIndex + 1);
    else if (event.key === "k" || event.key === "ArrowUp" && listIndex >= 0) selectItem(listIndex - 1);
    else if ((event.key === "Enter" || event.key === "o") && current
             && !(event.key === "Enter" && /^(BUTTON|A|SUMMARY)$/.test((document.activeElement || {}).tagName))) {
      var link = current.querySelector("a[href]");
      if (!link) return;
      window.location.href = link.href;
    } else if (event.key === "p" && current && current.querySelector("[data-key-pin]")) {
      current.querySelector("[data-key-pin]").click();
    } else if (event.key === "e" && current && current.querySelector("[data-key-done]")) {
      current.querySelector("[data-key-done]").click();
    } else return;
    event.preventDefault();
  });

  // A page with one answer box ([data-composer-keys]): the quick answers by key.
  document.addEventListener("keydown", function (event) {
    var scope = document.querySelector("[data-composer-keys]");
    if (!scope || !plainKey(event)) return;
    var pick = null;
    if (/^[1-9]$/.test(event.key)) pick = 'button[name="option"][value="' + (parseInt(event.key, 10) - 1) + '"]';
    else if (event.key === "y") pick = 'button[value="recommendation"]';
    else if (event.key === "d") pick = 'button[value="delegate"]';
    else if (event.key === "e") pick = 'button[value="done"]';
    else if (event.key === "a") {
      var box = scope.querySelector('textarea[name="answer_text"]');
      if (box) { event.preventDefault(); box.focus(); }
      return;
    } else if (event.key === "c") pick = ".copy-command [data-copy]";
    var button = pick && scope.querySelector(pick);
    if (!button) return;
    event.preventDefault();
    button.click();
  });

  // --- Theme ---

  var THEMES = ["system", "light", "dark"];

  function currentTheme() {
    return document.documentElement.dataset.theme || "system";
  }

  function applyTheme(theme) {
    if (theme === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = theme;
    try {
      if (theme === "system") localStorage.removeItem("apo-theme");
      else localStorage.setItem("apo-theme", theme);
    } catch (e) {}
    syncThemeToggle();
  }

  function syncThemeToggle() {
    var btn = document.querySelector("[data-theme-toggle]");
    if (!btn) return;
    var theme = currentTheme();
    var label = "Color theme: " + theme;
    btn.setAttribute("aria-label", label);
    btn.title = label + " (click to change)";
    btn.querySelectorAll("[data-theme-icon]").forEach(function (el) {
      el.hidden = el.dataset.themeIcon !== theme;
    });
  }

  function initThemeToggle() {
    var btn = document.querySelector("[data-theme-toggle]");
    if (!btn) return;
    syncThemeToggle();
    btn.addEventListener("click", function () {
      var next = THEMES[(THEMES.indexOf(currentTheme()) + 1) % THEMES.length];
      applyTheme(next);
      announce("Color theme: " + next);
    });
  }

  // --- Confirm dialog for destructive forms (data-confirm="question") ---

  var confirmDialog = null;

  function ensureConfirmDialog() {
    if (confirmDialog) return confirmDialog;
    confirmDialog = document.createElement("dialog");
    confirmDialog.className = "modal";
    confirmDialog.setAttribute("aria-labelledby", "confirm-dialog-title");
    confirmDialog.innerHTML =
      '<form method="dialog" class="modal-body">' +
      '<div class="modal-icon tone-danger">' + iconSvg("alert") + "</div>" +
      '<div><h2 id="confirm-dialog-title">Are you sure?</h2><p class="modal-text"></p></div>' +
      '<div class="modal-actions"><button value="cancel" class="btn btn--secondary">Cancel</button>' +
      '<button value="confirm" class="btn btn--danger-solid">Confirm</button></div></form>';
    confirmDialog.addEventListener("click", function (event) {
      if (event.target === confirmDialog) confirmDialog.close("cancel");
    });
    document.body.appendChild(confirmDialog);
    return confirmDialog;
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    // A submit button can carry its own question (bulk bar: only Cancel asks).
    var question = (event.submitter && event.submitter.dataset && event.submitter.dataset.confirm) ||
      (form.dataset && form.dataset.confirm);
    if (!question) return;
    if (form.dataset.confirmed === "1") {
      delete form.dataset.confirmed;
      return;
    }
    var dialog = ensureConfirmDialog();
    if (typeof dialog.showModal !== "function") {
      if (!window.confirm(question)) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    var submitter = event.submitter || submitButtonFor(form);
    dialog.querySelector(".modal-text").textContent = question;
    dialog.querySelector('[value="confirm"]').textContent = (submitter && submitter.textContent.trim()) || "Confirm";
    dialog.returnValue = "";
    dialog.addEventListener("close", function onClose() {
      dialog.removeEventListener("close", onClose);
      if (dialog.returnValue !== "confirm") return;
      form.dataset.confirmed = "1";
      resubmit(form, submitter);
    });
    dialog.showModal();
    dialog.querySelector('[value="cancel"]').focus();
  }, true);

  // --- Select all for the bulk bar (rows are checkboxes bound to the form by id) ---

  document.addEventListener("change", function (event) {
    var box = event.target;
    if (!box.matches || !box.matches("[data-select-all]")) return;
    var id = box.dataset.selectAll.replace(/^#/, "");
    document.querySelectorAll('input[name="ids"][form="' + id + '"]').forEach(function (row) { row.checked = box.checked; });
  });

  // The bulk bar stays out of the way until a row is ticked, then says how many.
  function syncBulk() {
    document.querySelectorAll("[data-bulk]").forEach(function (wrap) {
      var form = wrap.querySelector("form");
      if (!form) return;
      var n = document.querySelectorAll('input[name="ids"][form="' + form.id + '"]:checked').length;
      wrap.classList.add("is-scripted");
      wrap.hidden = n === 0;
      wrap.open = n > 0;
      var count = wrap.querySelector("[data-bulk-count]");
      if (count) count.textContent = n + " selected";
    });
  }
  document.addEventListener("change", function (event) {
    if (event.target.matches && event.target.matches('input[name="ids"], [data-select-all]')) syncBulk();
  });
  document.addEventListener("click", function (event) {
    var clear = event.target.closest && event.target.closest("[data-bulk-clear]");
    if (!clear) return;
    var id = clear.closest("form").id;
    document.querySelectorAll('input[name="ids"][form="' + id + '"], [data-select-all="#' + id + '"]').forEach(function (b) { b.checked = false; });
    syncBulk();
  });
  document.addEventListener("app:enhance", syncBulk);

  // Pop-up menus (a row's …, Filter, Snooze) close when you click elsewhere, open another
  // one, or press Escape, like a menu rather than a section that stays unfolded.
  function openPopups() {
    return document.querySelectorAll("details[open]:has(> .menu-pop), details[open].now-snooze");
  }
  document.addEventListener("click", function (event) {
    openPopups().forEach(function (menu) { if (!menu.contains(event.target)) menu.open = false; });
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    openPopups().forEach(function (menu) {
      var hadFocus = menu.contains(document.activeElement);
      menu.open = false;
      if (hadFocus) menu.querySelector("summary").focus();
    });
  });

  // Picking A bug in New work opens its folded fields: what happened goes there.
  document.addEventListener("change", function (event) {
    var kind = event.target;
    if (!kind.matches || !kind.matches('.new-work input[name="kind"]') || kind.value !== "bug") return;
    var more = kind.form && kind.form.querySelector(".new-work-more");
    if (more) more.open = true;
  });

  // --- Lightbox for attachment thumbnails ---

  var lightbox = null;

  function ensureLightbox() {
    if (lightbox) return lightbox;
    lightbox = document.createElement("dialog");
    lightbox.className = "lightbox";
    lightbox.setAttribute("aria-label", "Screenshot preview");
    lightbox.innerHTML =
      '<figure><img alt=""><figcaption><span class="lightbox-name"></span>' +
      '<span><a class="btn btn--ghost btn--sm lightbox-open" target="_blank" rel="noopener">' + iconSvg("external") + "Open original</a>" +
      '<button type="button" class="btn btn--ghost btn--sm lightbox-close">' + iconSvg("x") + "Close</button></span></figcaption></figure>";
    lightbox.querySelector(".lightbox-close").addEventListener("click", function () { lightbox.close(); });
    lightbox.addEventListener("click", function (event) {
      if (event.target === lightbox) lightbox.close();
    });
    document.body.appendChild(lightbox);
    return lightbox;
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-attachment-open]");
    if (!trigger) return;
    var dialog = ensureLightbox();
    if (typeof dialog.showModal !== "function") {
      window.open(trigger.dataset.src, "_blank", "noopener");
      return;
    }
    var img = dialog.querySelector("img");
    img.src = trigger.dataset.src;
    img.alt = trigger.dataset.filename || "Screenshot";
    dialog.querySelector(".lightbox-name").textContent = trigger.dataset.filename || "";
    dialog.querySelector(".lightbox-open").href = trigger.dataset.src;
    dialog.showModal();
  });

  // --- Attachment composer: paste / drag-drop / pick a screenshot onto a
  // decision, with optimistic thumbnails and no full-page reloads. Mirrors
  // the server-side limits in db.py (ATTACHMENT_MAX_BYTES / _PER_DECISION)
  // so most rejections never need a round trip.

  var ATTACHMENT_ACCEPT_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
  var ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024;
  var ATTACHMENT_MAX_PER_DECISION = 6;

  function attachmentsGridFor(container) {
    return container.querySelector("[data-attachments-grid]");
  }

  function attachmentCountFor(container) {
    var grid = attachmentsGridFor(container);
    return grid ? grid.querySelectorAll(".attachment").length : 0;
  }

  function updateAddControlVisibility(container) {
    var dropzone = container.querySelector("[data-attachment-dropzone]");
    if (!dropzone) return;
    dropzone.hidden = attachmentCountFor(container) >= ATTACHMENT_MAX_PER_DECISION;
  }

  function eventHasFiles(event) {
    var dt = event.dataTransfer;
    if (!dt || !dt.types) return false;
    for (var i = 0; i < dt.types.length; i++) {
      if (dt.types[i] === "Files") return true;
    }
    return false;
  }

  function validateAttachmentFile(file) {
    var name = file.name || "That file";
    if (ATTACHMENT_ACCEPT_TYPES.indexOf(file.type) === -1) {
      return name + " isn't a supported image type (PNG, JPEG, GIF, or WEBP).";
    }
    if (file.size > ATTACHMENT_MAX_BYTES) {
      return name + " is larger than the 10MB limit.";
    }
    return null;
  }

  function thumbClass(container) {
    return "attachment-thumb" + (container.dataset.thumbSize === "compact" ? " attachment-thumb--compact" : "");
  }

  function makePlaceholderNode(container) {
    var el = document.createElement("div");
    el.className = "attachment attachment--pending";
    var thumb = document.createElement("div");
    thumb.className = thumbClass(container) + " attachment-thumb--pending";
    thumb.setAttribute("aria-hidden", "true");
    var spinner = document.createElement("span");
    spinner.className = "spinner";
    thumb.appendChild(spinner);
    el.appendChild(thumb);
    return el;
  }

  function bindRemoveButton(container, btn) {
    btn.addEventListener("click", function () {
      var node = btn.closest(".attachment");
      if (node) removeAttachment(container, node);
    });
  }

  function makeAttachmentNode(container, attachment) {
    var name = attachment.filename || "Attached screenshot";
    var el = document.createElement("div");
    el.className = "attachment";
    el.dataset.attachmentId = attachment.id;

    var open = document.createElement("button");
    open.type = "button";
    open.className = "attachment-open";
    open.setAttribute("data-attachment-open", "");
    open.dataset.src = attachment.url;
    open.dataset.filename = name;
    open.setAttribute("aria-label", "View " + name);
    var img = document.createElement("img");
    img.className = thumbClass(container);
    img.src = attachment.url;
    img.alt = name;
    open.appendChild(img);
    el.appendChild(open);

    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "attachment-remove";
    btn.setAttribute("data-attachment-remove", "");
    btn.setAttribute("aria-label", "Remove " + name);
    btn.innerHTML = iconSvg("x");
    el.appendChild(btn);

    bindRemoveButton(container, btn);
    return el;
  }

  function removeAttachment(container, node) {
    var decisionId = container.dataset.decisionId;
    var attachmentId = node.dataset.attachmentId;
    var btn = node.querySelector(".attachment-remove");
    if (btn) btn.disabled = true;
    node.style.opacity = ".5";

    fetch("/decisions/" + decisionId + "/attachments/" + attachmentId, { method: "DELETE" })
      .then(function (response) {
        if (!response.ok && response.status !== 204) throw new Error("Couldn't remove that screenshot.");
        node.remove();
        updateAddControlVisibility(container);
        announce("Screenshot removed.");
      })
      .catch(function (err) {
        if (btn) btn.disabled = false;
        node.style.opacity = "";
        toast((err && err.message) || "Couldn't remove that screenshot.", "error");
      });
  }

  function uploadSingleFile(container, file) {
    var grid = attachmentsGridFor(container);
    if (!grid) return;
    var decisionId = container.dataset.decisionId;
    var placeholder = makePlaceholderNode(container);
    grid.appendChild(placeholder);
    updateAddControlVisibility(container);

    var body = new FormData();
    body.append("files", file);

    fetch("/decisions/" + decisionId + "/attachments", { method: "POST", body: body })
      .then(function (response) {
        return response
          .json()
          .catch(function () { return null; })
          .then(function (data) { return { response: response, data: data }; });
      })
      .then(function (result) {
        if (!result.response.ok) {
          var message = (result.data && result.data.detail) || "Couldn't upload that screenshot.";
          throw new Error(message);
        }
        var attachment = result.data && result.data[0];
        if (!attachment) throw new Error("Couldn't upload that screenshot.");
        placeholder.replaceWith(makeAttachmentNode(container, attachment));
        updateAddControlVisibility(container);
        announce((attachment.filename || "Screenshot") + " attached.");
      })
      .catch(function (err) {
        placeholder.remove();
        updateAddControlVisibility(container);
        toast((err && err.message) || "Couldn't upload that screenshot.", "error");
      });
  }

  function uploadAttachmentFiles(container, fileListLike) {
    var files = Array.prototype.slice.call(fileListLike || []);
    if (!files.length) return;

    var remainingSlots = ATTACHMENT_MAX_PER_DECISION - attachmentCountFor(container);
    if (remainingSlots <= 0) {
      toast("This decision already has the maximum of " + ATTACHMENT_MAX_PER_DECISION + " screenshots.", "error");
      return;
    }
    if (files.length > remainingSlots) {
      toast(
        "Only " + remainingSlots + " more screenshot" + (remainingSlots === 1 ? "" : "s") +
          " can be added to this decision.",
        "error"
      );
      files = files.slice(0, remainingSlots);
    }

    files.forEach(function (file) {
      var error = validateAttachmentFile(file);
      if (error) {
        toast(error, "error");
        return;
      }
      uploadSingleFile(container, file);
    });
  }

  function initAttachmentComposer(container) {
    if (!container || container.dataset.attachmentBound === "1") return;
    container.dataset.attachmentBound = "1";

    var grid = attachmentsGridFor(container);
    if (!grid) return;

    var existingRemoveButtons = grid.querySelectorAll("[data-attachment-remove]");
    for (var i = 0; i < existingRemoveButtons.length; i++) bindRemoveButton(container, existingRemoveButtons[i]);

    var input = container.querySelector("[data-attachment-input]");
    var canAdd = !!input;

    if (canAdd) {
      input.addEventListener("change", function () {
        uploadAttachmentFiles(container, input.files);
        input.value = "";
      });

      // Only intercept paste when the clipboard actually carries an image —
      // otherwise do nothing and let normal text pasting (e.g. into the
      // answer textarea, wherever focus happens to be inside this container)
      // proceed exactly as it would without this listener.
      container.addEventListener("paste", function (event) {
        var items = (event.clipboardData && event.clipboardData.items) || [];
        var imageFiles = [];
        for (var i = 0; i < items.length; i++) {
          if (items[i].kind === "file" && items[i].type && items[i].type.indexOf("image/") === 0) {
            var file = items[i].getAsFile();
            if (file) imageFiles.push(file);
          }
        }
        if (!imageFiles.length) return;
        event.preventDefault();
        uploadAttachmentFiles(container, imageFiles);
      });

      var dragDepth = 0;
      container.addEventListener("dragenter", function (event) {
        if (!eventHasFiles(event)) return;
        dragDepth++;
        container.classList.add("is-drag-over");
      });
      container.addEventListener("dragover", function (event) {
        if (!eventHasFiles(event)) return;
        event.preventDefault();
      });
      container.addEventListener("dragleave", function () {
        dragDepth = Math.max(0, dragDepth - 1);
        if (dragDepth === 0) container.classList.remove("is-drag-over");
      });
      container.addEventListener("drop", function (event) {
        dragDepth = 0;
        container.classList.remove("is-drag-over");
        if (!eventHasFiles(event)) return;
        event.preventDefault();
        uploadAttachmentFiles(container, event.dataTransfer.files);
      });
    }

    updateAddControlVisibility(container);
  }

  // --- Screenshots on New work: the item doesn't exist until the form is sent, so
  // pasted, dropped and picked images wait in the form's own file input and go up
  // with it. Without JS the input still works as a plain file picker.

  function makeStagedNode(file, onRemove) {
    var name = file.name || "Screenshot";
    var url = URL.createObjectURL(file);
    var el = document.createElement("div");
    el.className = "attachment";

    var open = document.createElement("button");
    open.type = "button";
    open.className = "attachment-open";
    open.setAttribute("data-attachment-open", "");
    open.dataset.src = url;
    open.dataset.filename = name;
    open.setAttribute("aria-label", "View " + name);
    var img = document.createElement("img");
    img.className = "attachment-thumb attachment-thumb--compact";
    img.src = url;
    img.alt = name;
    open.appendChild(img);
    el.appendChild(open);

    var remove = document.createElement("button");
    remove.type = "button";
    remove.className = "attachment-remove";
    remove.setAttribute("aria-label", "Remove " + name);
    remove.innerHTML = iconSvg("x");
    remove.addEventListener("click", onRemove);
    el.appendChild(remove);
    return el;
  }

  function initScreenshotPicker(box) {
    if (box.dataset.screenshotBound === "1") return;
    box.dataset.screenshotBound = "1";
    var form = box.closest("form");
    var input = box.querySelector("[data-screenshot-input]");
    var grid = box.querySelector("[data-screenshot-grid]");
    if (!form || !input || !grid || typeof DataTransfer !== "function") return;
    var staged = [];

    // Hidden for "Me", where there's no agent to look at them.
    function offered() {
      return input.closest("label").getClientRects().length > 0;
    }

    function render() {
      var held = new DataTransfer();
      staged.forEach(function (file) { held.items.add(file); });
      input.files = held.files;
      grid.querySelectorAll("[data-attachment-open]").forEach(function (b) { URL.revokeObjectURL(b.dataset.src); });
      grid.innerHTML = "";
      staged.forEach(function (file, index) {
        grid.appendChild(makeStagedNode(file, function () {
          staged.splice(index, 1);
          render();
          announce("Screenshot removed.");
        }));
      });
    }

    function add(fileListLike) {
      var files = Array.prototype.slice.call(fileListLike || []);
      files.forEach(function (file) {
        var error = validateAttachmentFile(file);
        if (error) return toast(error, "error");
        if (staged.length >= ATTACHMENT_MAX_PER_DECISION) {
          return toast("Up to " + ATTACHMENT_MAX_PER_DECISION + " screenshots.", "error");
        }
        staged.push(file);
      });
      render();
    }

    // Picking replaces the input's files: take the new ones, then put the whole set back.
    input.addEventListener("change", function () { add(input.files); });

    form.addEventListener("paste", function (event) {
      var items = (event.clipboardData && event.clipboardData.items) || [];
      var images = [];
      for (var i = 0; i < items.length; i++) {
        if (items[i].kind === "file" && items[i].type.indexOf("image/") === 0) {
          var file = items[i].getAsFile();
          if (file) images.push(file);
        }
      }
      if (!images.length || !offered()) return;
      event.preventDefault();
      add(images);
    });

    var dragDepth = 0;
    box.addEventListener("dragenter", function (event) {
      if (!eventHasFiles(event) || !offered()) return;
      dragDepth++;
      box.classList.add("is-drag-over");
    });
    box.addEventListener("dragover", function (event) {
      if (eventHasFiles(event) && offered()) event.preventDefault();
    });
    box.addEventListener("dragleave", function () {
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) box.classList.remove("is-drag-over");
    });
    box.addEventListener("drop", function (event) {
      dragDepth = 0;
      box.classList.remove("is-drag-over");
      if (!eventHasFiles(event) || !offered()) return;
      event.preventDefault();
      add(event.dataTransfer.files);
    });
  }

  function autoEnhanceAttachments(root) {
    var composers = root.querySelectorAll("[data-attachment-composer]");
    for (var i = 0; i < composers.length; i++) initAttachmentComposer(composers[i]);
    root.querySelectorAll("[data-screenshot-picker]").forEach(initScreenshotPicker);
  }

  // A link to a folded section (#notes on an item) opens it.
  function openHashTarget() {
    var id = location.hash.slice(1);
    var target = id && document.getElementById(id);
    var fold = target && (target.tagName === "DETAILS" ? target : target.closest("details"));
    if (fold) fold.open = true;
  }
  window.addEventListener("hashchange", openHashTarget);

  document.addEventListener("DOMContentLoaded", function () {
    ensureLiveRegions();
    initThemeToggle();
    autoEnhance(document);
    consumeFlash();
    consumeUndo();
    openHashTarget();
  });

  window.App = {
    toast: toast,
    announce: announce,
    setLoading: setLoading,
    clearLoading: clearLoading,
    enhanceForm: enhanceForm,
    indicateSubmit: indicateSubmit,
    autoEnhance: autoEnhance,
    initAttachmentComposer: initAttachmentComposer,
    initRecapToggles: initRecapToggles,
    onChange: onChange,
  };
})();
