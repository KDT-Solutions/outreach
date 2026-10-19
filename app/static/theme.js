// Colour scheme: auto (follows the system), light or dark. The choice is kept per
// browser in localStorage; without storage the page simply follows the system.
(function () {
  var KEY = "outreach-theme";
  var LABELS = { auto: "Auto", light: "Hell", dark: "Dunkel" };
  var NEXT = { auto: "light", light: "dark", dark: "auto" };

  function load() {
    try { var v = localStorage.getItem(KEY); return LABELS[v] ? v : "auto"; } catch (e) { return "auto"; }
  }
  function save(v) {
    try { if (v === "auto") localStorage.removeItem(KEY); else localStorage.setItem(KEY, v); } catch (e) {}
  }
  function apply(v) {
    var root = document.documentElement;
    if (v === "auto") root.removeAttribute("data-theme"); else root.setAttribute("data-theme", v);
    var b = document.getElementById("theme-toggle");
    if (b) {
      b.textContent = LABELS[v];
      b.title = "Darstellung: " + LABELS[v] + " (klicken zum Wechseln)";
    }
  }

  var current = load();
  apply(current); // runs in <head>, before the first paint
  document.addEventListener("DOMContentLoaded", function () {
    var b = document.getElementById("theme-toggle");
    if (!b) return;
    apply(current);
    b.addEventListener("click", function () {
      current = NEXT[current];
      save(current);
      apply(current);
    });
  });
})();
