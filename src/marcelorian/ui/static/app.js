// Forms carry the CSRF token as a hidden field. Placeholder for HTMX wiring in later phases.

// Verification queue keyboard shortcuts: each element with data-key is clicked when its
// key is pressed, unless the applicant is typing in a field.
(function () {
  "use strict";
  var root = document.querySelector("[data-shortcuts]");
  if (!root) {
    return;
  }
  document.addEventListener("keydown", function (event) {
    if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) {
      return;
    }
    var target = event.target;
    var tag = target && target.tagName ? target.tagName.toLowerCase() : "";
    if (tag === "input" || tag === "textarea" || tag === "select" || (target && target.isContentEditable)) {
      return;
    }
    var key = (event.key || "").toLowerCase();
    var element = root.querySelector('[data-key="' + key.replace(/["\\]/g, "") + '"]');
    if (!element) {
      return;
    }
    event.preventDefault();
    element.click();
  });
})();

// File uploads: a multipart form cannot carry the CSRF header, so forms marked
// data-upload are sent with fetch and the X-CSRF-Token header from the page meta tag.
(function () {
  "use strict";
  var meta = document.querySelector('meta[name="csrf-token"]');
  document.querySelectorAll("form[data-upload]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var button = form.querySelector("button[type=submit]");
      if (button) {
        button.disabled = true;
      }
      fetch(form.action, {
        method: "POST",
        body: new FormData(form),
        headers: { "X-CSRF-Token": meta ? meta.getAttribute("content") : "" },
        credentials: "same-origin"
      }).then(function (response) {
        window.location.assign(response.url || window.location.href);
      }).catch(function () {
        window.location.reload();
      });
    });
  });
})();

// "Copy text" on the hand-off page.
(function () {
  "use strict";
  document.querySelectorAll("[data-copy-button]").forEach(function (button) {
    button.addEventListener("click", function () {
      var area = document.querySelector("[data-copy]");
      if (!area) {
        return;
      }
      area.select();
      var done = function () { button.textContent = "Copied"; };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(area.value).then(done, function () { document.execCommand("copy"); done(); });
      } else {
        document.execCommand("copy");
        done();
      }
    });
  });
})();

// Notices: the bar under the header lists what needs the applicant now. It refreshes every
// minute, shows the count of action items in the tab title, and, when the applicant turned
// desktop alerts on, raises a browser notification for a notice that is new or grew.
// Text is set with textContent only; links are same-site paths from the server.
(function () {
  "use strict";
  var bar = document.getElementById("notices");
  if (!bar || !window.fetch) {
    return;
  }
  var baseTitle = document.title;
  var seen = {};
  var first = true;

  function alertsOn() {
    return "Notification" in window && Notification.permission === "granted";
  }

  function render(data) {
    bar.textContent = "";
    (data.notices || []).forEach(function (notice) {
      var link = document.createElement("a");
      link.className = "notice notice-" + notice.level;
      link.href = notice.url.charAt(0) === "/" ? notice.url : "/";
      link.textContent = notice.text;
      bar.appendChild(link);
      var before = seen[notice.key] || 0;
      if (!first && notice.count > before && alertsOn()) {
        new Notification("Marcelorian", { body: notice.text, tag: notice.key });
      }
      seen[notice.key] = notice.count;
    });
    if ("Notification" in window && Notification.permission === "default" && data.notices && data.notices.length) {
      var ask = document.createElement("button");
      ask.type = "button";
      ask.className = "notice-alerts";
      ask.textContent = "Turn on desktop alerts";
      ask.addEventListener("click", function () {
        Notification.requestPermission().then(function () { ask.remove(); });
      });
      bar.appendChild(ask);
    }
    bar.hidden = !bar.childNodes.length;
    document.title = data.actions ? "(" + data.actions + ") " + baseTitle : baseTitle;
    first = false;
  }

  function refresh() {
    fetch("/api/notices", { credentials: "same-origin", cache: "no-store" })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) { if (data) { render(data); } })
      .catch(function () {});
  }

  refresh();
  window.setInterval(refresh, 60000);
})();
