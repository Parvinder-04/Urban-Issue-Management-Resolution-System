/* UIMRS shared navigation behaviour — Task 15.
   Small, dependency-free helper used by the landing page, analytics page and
   the 3D gallery. It only controls the mobile menu and the current-page
   highlight; no data is fetched here. */
(function () {
  "use strict";

  function ready(fn) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", fn);
    } else {
      fn();
    }
  }

  ready(function () {
    var toggle = document.querySelector("[data-uimrs-nav-toggle]");
    var links = document.querySelector("[data-uimrs-nav-links]");
    if (toggle && links) {
      toggle.addEventListener("click", function () {
        var open = links.classList.toggle("open");
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      });
      links.addEventListener("click", function (event) {
        if (event.target && event.target.tagName === "A") {
          links.classList.remove("open");
          toggle.setAttribute("aria-expanded", "false");
        }
      });
      document.addEventListener("keydown", function (event) {
        if (event.key === "Escape") {
          links.classList.remove("open");
          toggle.setAttribute("aria-expanded", "false");
        }
      });
    }

    // Highlight the navigation entry for the current path. Pages can override
    // with data-nav-current on <body>.
    var current = document.body.getAttribute("data-nav-current");
    if (!current) {
      var path = window.location.pathname.replace(/\/+$/, "") || "/";
      if (path === "/") current = "home";
      else if (path.indexOf("/dashboard") === 0) current = "dashboard";
      else if (path.indexOf("/citizen") === 0) current = "citizen";
      else if (path.indexOf("/officer") === 0) current = "officer";
      else if (path.indexOf("/admin") === 0) current = "admin";
      else if (path.indexOf("/analytics") === 0) current = "analytics";
      else if (path.indexOf("/gallery") === 0) current = "gallery";
    }
    if (current) {
      var match = document.querySelector('[data-nav="' + current + '"]');
      if (match) match.classList.add("active");
    }
  });
})();
