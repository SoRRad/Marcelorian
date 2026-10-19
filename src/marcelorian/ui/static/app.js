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
