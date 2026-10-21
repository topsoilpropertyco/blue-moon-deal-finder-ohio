/* The /login page (bluemoon/web/auth.py LOGIN_PAGE). A file, not an inline <script>, so the
   Content-Security-Policy can forbid inline scripts. /static/ is served before login. */
document.getElementById("f").addEventListener("submit", async function (e) {
  e.preventDefault();
  var err = document.getElementById("err"); err.textContent = "";
  var em = document.getElementById("em");
  var body = {password: document.getElementById("pw").value};
  if (em) body.email = em.value.trim();
  try {
    var r = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body), credentials: "same-origin"});
    if (r.ok) { location.replace("/" + location.hash); return; }
    var j = await r.json().catch(function () { return {}; });
    if (j.email && !em) { location.reload(); return; }   // the team list changed: show the email box
    err.textContent = j.detail || "That didn't work. Try again.";
  } catch (x) { err.textContent = "Can't reach the dashboard. Check your internet, then try again."; }
});
