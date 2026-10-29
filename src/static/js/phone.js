// The phone's two bits of behaviour. Both are conveniences over plain forms:
// without this script the quantities are ordinary number fields and the stop
// is an ordinary button, and the server's rules are the same either way.
(function () {
  // -- the tradesheet's steppers -------------------------------------------
  //
  // Arithmetic for the reader only: what the change would cost at the prices
  // the plan was sized at, and what would be left. The server re-checks all
  // of it (`orders.overrides.adjust_line`), so nothing here is trusted.
  var form = document.getElementById("trade");
  if (form) {
    var spendable = parseFloat(form.getAttribute("data-spendable")) || 0;
    var lines = Array.prototype.slice.call(form.querySelectorAll("[data-line]"));
    var cost = form.querySelector("[data-cost]");
    var carry = form.querySelector("[data-carry]");
    var warn = form.querySelector("[data-warn]");
    var reason = form.querySelector("[data-reason]");
    var total = document.querySelector("[data-total]");
    var primary = document.querySelector("[data-primary]");
    var sizedNote = warn ? warn.textContent : "";

    var money = function (v) {
      var s = Math.abs(v).toFixed(2).split(".");
      var x = s[0];
      var last = x.length > 3 ? x.slice(-3) : x;
      var rest = x.length > 3 ? x.slice(0, -3) : "";
      if (rest) {
        last = rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + last;
      }
      return (v < 0 ? "−₹" : "₹") + last + "." + s[1];
    };

    var refresh = function () {
      var sum = 0;
      var shares = 0;
      var changed = false;
      var below = false;
      lines.forEach(function (line) {
        var input = line.querySelector("input");
        if (!input) {
          return;
        }
        var qty = parseInt(input.value, 10);
        if (isNaN(qty) || qty < 0) {
          qty = 0;
        }
        sum += qty * (parseFloat(line.getAttribute("data-price")) || 0);
        shares += qty;
        changed = changed || qty !== parseInt(line.getAttribute("data-planned"), 10);
        below = below || qty < parseInt(line.getAttribute("data-floor"), 10);
      });
      var left = spendable - sum;
      var over = left < 0;
      if (cost) { cost.textContent = money(sum); }
      if (carry) {
        carry.textContent = money(left);
        carry.classList.toggle("text-bad", over);
        carry.classList.toggle("text-accent", !over);
      }
      if (total) { total.textContent = shares; }
      if (reason) { reason.hidden = !changed; }
      if (warn) {
        warn.textContent = over
          ? "That is more than today has to spend. Take a share off, or add a lumpsum first."
          : below
            ? "A name cannot go below what has already filled — a fill is never unwound."
            : changed
              ? "Changed from what the system chose. The override is recorded with your name and reason against it."
              : sizedNote;
        warn.classList.toggle("text-bad", over || below);
        warn.classList.toggle("text-accent", changed && !over && !below);
        warn.classList.toggle("text-ink-4", !changed);
      }
      if (primary) {
        primary.disabled = over || below || (primary.value === "save" && !changed);
        primary.classList.toggle("opacity-40", primary.disabled);
      }
    };

    lines.forEach(function (line) {
      var input = line.querySelector("input");
      if (!input) {
        return;
      }
      Array.prototype.forEach.call(line.querySelectorAll("[data-step]"), function (button) {
        button.addEventListener("click", function () {
          var next = (parseInt(input.value, 10) || 0) + parseInt(button.getAttribute("data-step"), 10);
          input.value = Math.max(0, next);
          refresh();
        });
      });
      input.addEventListener("input", refresh);
    });
    refresh();
  }

  // -- sign-in, in two steps -----------------------------------------------
  //
  // One form and one submission; the steps are only what is on screen. A
  // failed attempt comes back with both steps open, because the server does
  // not say which factor was wrong and neither should the page. The username
  // and the step survive the tab being reclaimed; the password never is kept.
  var signin = document.getElementById("signin");
  if (signin) {
    var one = signin.querySelector('[data-step="1"]');
    var two = signin.querySelector('[data-step="2"]');
    var marks = signin.querySelectorAll("[data-step-mark]");
    var title = signin.querySelector("[data-title]");
    var subtitle = signin.querySelector("[data-subtitle]");
    var user = signin.querySelector('input[name="username"]');
    var password = signin.querySelector('input[name="password"]');
    var code = signin.querySelector('input[name="otp_token"]');
    var KEY = "signin";

    var remember = function (step) {
      try {
        sessionStorage.setItem(KEY, JSON.stringify({ step: step, username: user.value }));
      } catch (e) {}
    };
    var recall = function () {
      try {
        return JSON.parse(sessionStorage.getItem(KEY) || "null");
      } catch (e) {
        return null;
      }
    };
    var show = function (step) {
      var first = step === 1;
      one.hidden = !first;
      two.hidden = first;
      Array.prototype.forEach.call(marks, function (mark) {
        mark.classList.toggle("text-accent", mark.getAttribute("data-step-mark") === String(step));
      });
      title.textContent = first ? "Sign in" : "Enter your code";
      subtitle.textContent = first
        ? "Two factors, and neither one alone will do."
        : "Signing in as " + user.value + ".";
      remember(step);
      (first ? (user.value ? password : user) : code).focus();
    };

    if (!signin.hasAttribute("data-errors")) {
      signin.querySelector("[data-next]").classList.remove("hidden");
      signin.querySelector("[data-back]").classList.remove("hidden");
      var saved = recall();
      if (saved && saved.username && !user.value) {
        user.value = saved.username;
      }
      // The password is gone after a reload, so a reclaimed tab resumes at
      // the password with the username filled, never at the code.
      show(1);

      signin.querySelector("[data-next]").addEventListener("click", function () {
        if (user.reportValidity() && password.reportValidity()) {
          show(2);
        }
      });
      signin.querySelector("[data-back]").addEventListener("click", function () {
        show(1);
      });
      password.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          signin.querySelector("[data-next]").click();
        }
      });
    }
    signin.addEventListener("submit", function () {
      try {
        sessionStorage.removeItem(KEY);
      } catch (e) {}
    });
  }

  // -- press and hold ------------------------------------------------------
  //
  // The cost of an accidental tap is a day, so the stop submits only after
  // the button has been held for the whole of `data-hold` milliseconds. A
  // keyboard (Enter, Space) still works: holding is about thumbs, not about
  // making the stop hard to reach.
  Array.prototype.forEach.call(document.querySelectorAll("[data-hold]"), function (button) {
    var ms = parseInt(button.getAttribute("data-hold"), 10) || 1500;
    var fill = button.querySelector("[data-hold-fill]");
    var timer = null;
    var armed = false;

    var reset = function () {
      clearTimeout(timer);
      timer = null;
      if (fill) {
        fill.style.transition = "none";
        fill.style.width = "0";
      }
    };
    var start = function (event) {
      if (event.button !== undefined && event.button !== 0) {
        return;
      }
      if (!button.form.reportValidity()) {
        return;
      }
      event.preventDefault();
      if (fill) {
        fill.style.transition = "width " + ms + "ms linear";
        fill.style.width = "100%";
      }
      timer = setTimeout(function () {
        armed = true;
        button.form.requestSubmit(button);
      }, ms);
    };

    button.addEventListener("pointerdown", start);
    ["pointerup", "pointerleave", "pointercancel"].forEach(function (name) {
      button.addEventListener(name, function () {
        if (!armed) {
          reset();
        }
      });
    });
    button.addEventListener("click", function (event) {
      // A pointer click never submits -- the hold's timer does -- and a
      // click with no pointer behind it (the keyboard) goes through.
      if (event.detail > 0) {
        event.preventDefault();
      }
    });
    button.addEventListener("contextmenu", function (event) {
      event.preventDefault();
    });
  });
})();
