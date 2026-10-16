// Exchanges the one-time bootstrap token (kept in the URL fragment so it never
// reaches server logs) for an HttpOnly session cookie, then opens the dashboard.
(function () {
  var match = /[#&]t=([A-Za-z0-9_-]+)/.exec(window.location.hash);
  history.replaceState(null, "", "/bootstrap");
  var status = document.getElementById("bootstrap-status");
  if (!match) { return; }
  fetch("/bootstrap/exchange", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: match[1] })
  }).then(function (response) {
    if (response.ok) { window.location.replace("/"); }
    else if (status) { status.textContent = "This sign-in link has expired. Open Marcelorian again from its tray icon."; }
  });
})();
