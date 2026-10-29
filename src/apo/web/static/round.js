// Lightning round keys: one card on screen, so every key acts on it.
(function () {
  var root = document.querySelector("[data-round]");
  if (!root) return;

  function press(selector) {
    var el = root.querySelector(selector);
    if (!el) return false;
    el.click();
    return true;
  }

  document.addEventListener("keydown", function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    var active = document.activeElement;
    if (active && (active.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName))) {
      if (event.key === "Escape") active.blur();
      return;
    }
    if (document.querySelector("dialog[open]")) return;
    if (event.key === "Enter" && active && /^(BUTTON|A|SUMMARY)$/.test(active.tagName)) return;
    var handled;
    if (event.key === "Enter") {
      handled = press("[data-now-primary]") || press('button[value="recommendation"]') || press('button[value="done"]');
    }
    else if (/^[1-9]$/.test(event.key)) handled = press('button[name="option"][value="' + (parseInt(event.key, 10) - 1) + '"]');
    else if (event.key === "d") handled = press('button[value="delegate"]');
    else if (event.key === "y") handled = press('button[value="recommendation"]');
    else if (event.key === "a") {
      var box = root.querySelector('textarea[name="answer_text"]');
      if (box) { box.focus(); handled = true; }
    }
    else if (event.key === "s") handled = press('.snooze-menu button[value="tomorrow"]');
    else if (event.key === "n" || event.key === "ArrowRight") handled = press("[data-round-skip]");
    else if (event.key === "Escape") handled = press("[data-round-exit]");
    if (handled) event.preventDefault();
  });
})();
