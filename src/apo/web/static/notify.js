// Browser notifications. Off until turned on in Settings (permission + a per-browser
// switch). Polls from every page, hidden tabs too — the server decides what's worth
// an interruption and records each one as sent, so a second tab never repeats it.
(function () {
  var KEY = "apo-notify";
  var POLL_MS = 60000;
  var shown = {};

  function supported() { return "Notification" in window; }
  function switchedOn() {
    try { return localStorage.getItem(KEY) === "on"; } catch (e) { return false; }
  }
  function setSwitch(on) {
    try { if (on) localStorage.setItem(KEY, "on"); else localStorage.removeItem(KEY); } catch (e) {}
  }
  function active() { return supported() && Notification.permission === "granted" && switchedOn(); }

  async function poll() {
    if (!active()) return;
    try {
      var res = await fetch("/api/notify", { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      var data = await res.json();
      (data.notify || []).forEach(function (n) {
        var note = new Notification(n.title, { body: n.body, tag: n.key });
        note.onclick = function () { window.focus(); window.location.href = n.href; note.close(); };
        shown[n.key] = note;
      });
      (data.retract || []).forEach(function (key) {
        if (shown[key]) { shown[key].close(); delete shown[key]; }
      });
    } catch (e) {}
  }

  function renderSettings() {
    var status = document.querySelector("[data-notify-status]");
    var button = document.querySelector("[data-notify-toggle]");
    if (!status || !button) return;
    if (!supported()) {
      status.textContent = "This browser doesn't support notifications.";
      button.hidden = true;
      return;
    }
    if (Notification.permission === "denied") {
      status.textContent = "Blocked for this site in the browser's settings — allow notifications there first.";
      button.hidden = true;
      return;
    }
    status.textContent = active() ? "On in this browser." : "Off in this browser.";
    button.textContent = active() ? "Turn off" : "Turn on";
    button.hidden = false;
  }

  document.addEventListener("click", async function (event) {
    var button = event.target.closest && event.target.closest("[data-notify-toggle]");
    if (!button) return;
    if (active()) {
      setSwitch(false);
    } else {
      var permission = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
      setSwitch(permission === "granted");
      if (permission === "granted") poll();
    }
    renderSettings();
  });

  renderSettings();
  poll();
  setInterval(poll, POLL_MS);
})();
