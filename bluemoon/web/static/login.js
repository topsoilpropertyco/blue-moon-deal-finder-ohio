/* The /login page (bluemoon/web/auth.py LOGIN_PAGE). A file, not an inline <script>, so the
   Content-Security-Policy can forbid inline scripts. /static/ is served before login. */
document.getElementById("f").addEventListener("submit", async function (e) {
  e.preventDefault();
  var err = document.getElementById("err"); err.textContent = "";
  try {
    var r = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({password: document.getElementById("pw").value}), credentials: "same-origin"});
    if (r.ok) { location.replace("/"); return; }
    var j = await r.json().catch(function () { return {}; });
    err.textContent = j.detail || "That didn't work. Try again.";
  } catch (x) { err.textContent = "Can't reach the app. Is it running?"; }
});
