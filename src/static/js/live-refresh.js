// A page's live region refreshes itself while the market is open.
//
// The server marks a region with data-live (the URL of its contents on their
// own) only on today's page and only in session, so a region without it is
// already the whole truth and nothing here runs. Each round the contents are
// fetched and swapped in -- server-rendered, no client state -- and the
// server's X-Live-Open header says when to stop asking.
//
// Never swapped from under the reader: not while the tab is hidden, not
// while focus or a text selection is inside the region, and not at all once
// the switch is off. The switch is this browser's setting, and remembered.
(function () {
  var regions = Array.prototype.slice.call(document.querySelectorAll("[data-live]"));
  if (!regions.length) {
    return;
  }

  var KEY = "live-refresh";
  var toggle = document.querySelector("[data-live-switch]");
  var dot = toggle && toggle.querySelector("[data-live-dot]");
  var label = toggle && toggle.querySelector("[data-live-label]");
  var every = 1000 * Math.max(10, parseInt(regions[0].getAttribute("data-live-every"), 10) || 30);
  var timer = null;
  var stopped = false;
  var on = read() !== "off";

  function read() {
    try {
      return localStorage.getItem(KEY);
    } catch (e) {
      return null;
    }
  }

  function write(value) {
    try {
      localStorage.setItem(KEY, value);
    } catch (e) {}
  }

  function stamp() {
    var now = new Date();
    return [now.getHours(), now.getMinutes(), now.getSeconds()]
      .map(function (n) { return String(n).padStart(2, "0"); })
      .join(":");
  }

  function show(text, tone) {
    if (!toggle) {
      return;
    }
    toggle.setAttribute("aria-pressed", on && !stopped ? "true" : "false");
    label.textContent = text;
    dot.classList.toggle("dot-good", tone === "good");
    dot.classList.toggle("dot-bad", tone === "bad");
    dot.style.background = tone ? "" : "var(--color-ink-4)";
  }

  function busy(region) {
    var active = document.activeElement;
    if (active && active !== document.body && region.contains(active)) {
      return true;
    }
    var selection = window.getSelection && window.getSelection();
    return !!(
      selection &&
      !selection.isCollapsed &&
      selection.rangeCount &&
      region.contains(selection.getRangeAt(0).commonAncestorContainer)
    );
  }

  function halt(text, tone) {
    stopped = true;
    clearTimeout(timer);
    if (toggle) {
      toggle.disabled = true;
    }
    show(text, tone);
  }

  function refresh(region) {
    return fetch(region.getAttribute("data-live"), {
      credentials: "same-origin",
      cache: "no-store",
      headers: { "X-Requested-With": "fetch" },
    }).then(function (response) {
      // Anything but the fragment -- a login page after the session lapsed,
      // an error page -- must not be pasted into the board.
      if (!response.ok || response.headers.get("X-Live-Fragment") !== "1") {
        throw new Error("not a live fragment");
      }
      var open = response.headers.get("X-Live-Open") !== "0";
      return response.text().then(function (html) {
        if (!busy(region)) {
          region.innerHTML = html;
        }
        return open;
      });
    });
  }

  function round() {
    clearTimeout(timer);
    if (stopped || !on) {
      return;
    }
    if (document.hidden) {
      schedule();
      return;
    }
    Promise.all(regions.map(refresh)).then(
      function (open) {
        if (open.indexOf(false) !== -1) {
          halt("MARKET CLOSED", "");
          return;
        }
        show("LIVE · " + stamp(), "good");
        schedule();
      },
      function () {
        halt("PAUSED · RELOAD", "bad");
      }
    );
  }

  function schedule() {
    timer = setTimeout(round, every);
  }

  if (toggle) {
    toggle.addEventListener("click", function () {
      on = !on;
      write(on ? "on" : "off");
      if (on) {
        show("LIVE", "good");
        round();
      } else {
        clearTimeout(timer);
        show("PAUSED", "");
      }
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && on && !stopped) {
      round();
    }
  });

  if (on) {
    show("LIVE", "good");
    schedule();
  } else {
    show("PAUSED", "");
  }
})();
