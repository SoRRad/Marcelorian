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
