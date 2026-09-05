(function () {
  var yearEl = document.getElementById("year");
  if (yearEl) yearEl.textContent = String(new Date().getFullYear());

  function applyLang(lang) {
    var next = lang === "zh" ? "zh" : "en";
    var root = document.documentElement;
    root.lang = next;
    root.setAttribute("data-ui-lang", next);
    try {
      localStorage.setItem("tuneya-lang", next);
    } catch (e) {}
    document.querySelectorAll("[data-set-lang]").forEach(function (btn) {
      btn.setAttribute("aria-pressed", btn.getAttribute("data-set-lang") === next ? "true" : "false");
    });
  }

  var saved = "en";
  try {
    saved = localStorage.getItem("tuneya-lang") || "en";
  } catch (e) {}
  applyLang(saved);

  document.querySelectorAll("[data-set-lang]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      applyLang(btn.getAttribute("data-set-lang"));
    });
  });
})();
