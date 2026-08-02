/*
 * Parser-blocking first-paint theme resolver. Keep this same-origin external
 * script so production's script-src 'self' CSP can execute it.
 */
(function () {
  var prefix = "frostvault_theme";
  var activeUserKey = "frostvault_theme_active_user";
  var guestKey = "frostvault_theme_guest";
  var preference = "system";
  var isLoginRoute = window.location.pathname === "/login";

  // A login screen has no trusted identity. Ignore and clear stale markers
  // before the browser paints the page, even if an earlier navigation failed.
  var activeUser = null;
  try {
    if (isLoginRoute) window.localStorage.removeItem(activeUserKey);
    else activeUser = window.localStorage.getItem(activeUserKey);
  } catch (_) {
    // A failed identity operation must not prevent reading the safe preference.
  }

  var key = activeUser
    ? prefix + "_user_" + encodeURIComponent(activeUser)
    : guestKey;
  try {
    var stored = window.localStorage.getItem(key);
    if (stored === "light" || stored === "dark" || stored === "system") {
      preference = stored;
    }
  } catch (_) {
    // Storage can be unavailable in private browsing; system remains safe.
  }

  var dark = preference === "dark";
  if (preference === "system") {
    try {
      dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    } catch (_) {
      dark = false;
    }
  }
  var theme = dark ? "dark" : "light";
  var root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  if (dark) root.classList.add("dark");
})();
